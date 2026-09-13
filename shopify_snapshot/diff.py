"""Diff: payloads da transformacao x o que ja existe na loja Shopify.

Le os payloads prontos de data/shopify.sqlite e o snapshot de
data/shopify_existing.sqlite, e grava no proprio data/shopify.sqlite:

* `existing` - uma linha por payload pronto, com a acao para a carga:
    create      nada correspondente na loja: criar
    update      ja existe e a mutation e upsert (productSet, customerSet,
                shopPolicyUpdate): a carga atualiza o registro pelo ID
    skip        ja existe: nao criar de novo; as referencias apontam para ele
    conflict    mais de um candidato ou dados que se contradizem: revisar
    unverified  nao deu para conferir (recurso sem permissao de leitura, ou
                pedidos sem read_all_orders): a carga NAO deve enviar
* `ref_map` - bagy-ref -> ID na Shopify dos registros que ja existem.

As duas tabelas sao refeitas a cada diff (o que a carga criar vai reaparecer no
proximo snapshot). So local: nada e enviado para a Shopify.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from bagy2shopify.transform import only_digits, strip_accents
from bagy_transform.common import strip_html

ACTIONS = ("create", "update", "skip", "conflict", "unverified")
UPSERT_MUTATIONS = {"productSet", "customerSet", "shopPolicyUpdate", "metaobjectUpsert", "metafieldsSet"}

# Entidade da transformacao -> recurso do snapshot sem o qual nao da para conferir.
REQUIRES = {
    "metaobject_definition": "metaobject_definitions",
    "metafield_definition": "metafield_definitions",
    "collection": "collections",
    "file": "files",
    "metaobject": "metaobjects",
    "product": "products",
    "product_content": "products",
    "customer": "customers",
    "lead": "customers",
    "store_credit": "customers",
    "discount": "discounts",
    "blog": "blogs",
    "article": "articles",
    "policy": "policies",
    "page": "pages",
    "menu": "menus",
    "redirect": "url_redirects",
    "order": "orders",
}
ENTITY_ORDER = list(REQUIRES)

# Sem read_all_orders a Shopify so devolve pedidos criados nos ultimos 60 dias.
ORDER_WINDOW_DAYS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS existing (
    entity       TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    action       TEXT NOT NULL,
    shopify_id   TEXT,
    match_by     TEXT,
    weak_match   INTEGER NOT NULL DEFAULT 0,
    candidates   TEXT,
    notes        TEXT,
    payload_hash TEXT,
    snapshot_at  TEXT,
    diff_at      TEXT NOT NULL,
    PRIMARY KEY (entity, source_key)
);
CREATE INDEX IF NOT EXISTS idx_existing_action ON existing (entity, action);

CREATE TABLE IF NOT EXISTS ref_map (
    ref        TEXT PRIMARY KEY,
    shopify_id TEXT NOT NULL,
    entity     TEXT,
    source_key TEXT,
    match_by   TEXT
);
"""


class DiffError(RuntimeError):
    pass


@dataclass
class Item:
    entity: str
    source_key: str
    mutation: str
    variables: dict
    post_actions: list
    provides: list
    hash: str
    meta: dict


@dataclass
class Row:
    entity: str
    source_key: str
    action: str = "create"
    shopify_id: str | None = None
    match_by: str | None = None
    weak: bool = False
    candidates: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    refs: dict = field(default_factory=dict)
    payload_hash: str | None = None

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)


@dataclass(frozen=True)
class Key:
    name: str           # vai para match_by
    value: Callable     # item -> valor (ou lista de valores) procurado no indice
    strong: bool = True


class Index:
    def __init__(self, records: list):
        self.by_gid = {record["id"]: record for record in records}
        self._maps = defaultdict(lambda: defaultdict(list))

    def add(self, name: str, value, gid: str) -> None:
        if value in (None, ""):
            return
        bucket = self._maps[name][value]
        if gid not in bucket:
            bucket.append(gid)

    def get(self, name: str, value) -> list:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        found = []
        for single in values:
            if single in (None, ""):
                continue
            for gid in self._maps[name].get(single, ()):
                if gid not in found:
                    found.append(gid)
        return found


# --------------------------------------------------------------------------- #
# Normalizacao de chaves

def lower(value) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def norm_text(value) -> str | None:
    if not value:
        return None
    text = re.sub(r"[^a-z0-9]+", " ", strip_accents(str(value)).lower()).strip()
    return text or None


def norm_sku(value) -> str | None:
    text = str(value or "").strip().upper()
    return text or None


def norm_path(value) -> str | None:
    if not value:
        return None
    parsed = urlparse(unquote(str(value).strip()))
    path = (parsed.path or "/").rstrip("/").lower() or "/"
    return path + (f"?{parsed.query.lower()}" if parsed.query else "")


def norm_body(value) -> str | None:
    text = strip_html(value)
    return " ".join(text.lower().split()) if text else None


def parse_instant(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def order_stamp(processed_at, total) -> str | None:
    """Data (ate o minuto, em UTC) + total: impressao digital do pedido."""
    moment, amount = parse_instant(processed_at), to_decimal(total)
    if moment is None or amount is None:
        return None
    return f"{moment.astimezone(timezone.utc):%Y-%m-%dT%H:%M}|{amount}"


def metafield_value(metafields, namespace: str, key: str):
    for metafield in metafields or []:
        if metafield.get("namespace") == namespace and metafield.get("key") == key:
            return metafield.get("value")
    return None


def core(item: Item) -> dict:
    """O objeto principal das variaveis (collection, page, article...)."""
    for value in item.variables.values():
        if isinstance(value, dict) and ("handle" in value or "code" in value or "path" in value):
            return value
    return item.variables


# --------------------------------------------------------------------------- #
# Casamento

def match(items: list, keys: list, index: Index, *, upsert: bool, identity=None, claimed=None) -> dict:
    """Casa cada payload com no maximo um registro da loja.

    Primeiro so as chaves fortes (ID da Bagy em metafield, handle, e-mail,
    codigo), para todos; depois as fracas (titulo, data+total) para quem sobrou,
    e so com registros que ninguem reivindicou.

    identity = (item -> id da Bagy, registro -> id da Bagy gravado nele). Um
    registro que ja carrega OUTRO id da Bagy nunca casa: se ele bloqueou uma
    chave forte (ex.: o handle), o payload vira conflito, porque criar ou
    atualizar por aquele handle mexeria no registro errado.
    """
    claimed = {} if claimed is None else claimed
    rows, hits, blocked = {}, {}, {}

    def compatible(item, gid) -> bool:
        if identity is None:
            return True
        mine, theirs = identity[0](item), identity[1](index.by_gid.get(gid) or {})
        return not (mine and theirs and str(mine) != str(theirs))

    for item in items:
        found, stopped = [], {}
        for key in keys:
            gids = index.get(key.name, key.value(item))
            allowed = [gid for gid in gids if compatible(item, gid)]
            if key.strong:
                stopped.update({gid: key.name for gid in gids if gid not in allowed})
            found.append((key, allowed))
        hits[item.source_key], blocked[item.source_key] = found, stopped

    for strong in (True, False):
        for item in items:
            if item.source_key in rows:
                continue
            for key, gids in hits[item.source_key]:
                if key.strong is not strong:
                    continue
                if not strong:
                    gids = [gid for gid in gids if gid not in claimed]
                if not gids:
                    continue
                row = Row(item.entity, item.source_key, match_by=key.name, weak=not strong,
                          payload_hash=item.hash)
                owner = claimed.get(gids[0])
                if len(gids) > 1:
                    row.action, row.candidates = "conflict", list(gids)
                    row.note(f"mais de um registro na Shopify com o mesmo {key.name}")
                elif owner and owner != (item.entity, item.source_key):
                    row.action, row.candidates = "conflict", list(gids)
                    row.note(f"registro da Shopify ja casado com outro payload pelo {key.name}")
                else:
                    row.action = "update" if upsert else "skip"
                    row.shopify_id = gids[0]
                    claimed[gids[0]] = (item.entity, item.source_key)
                    if not strong:
                        row.note(f"casou so por {key.name}: conferir")
                rows[item.source_key] = row
                break

    for item in items:
        row = rows.get(item.source_key)
        stopped = blocked[item.source_key]
        if row is None:
            row = rows[item.source_key] = Row(item.entity, item.source_key, payload_hash=item.hash)
            if stopped:
                row.action, row.candidates = "conflict", sorted(stopped)
                for name in sorted(set(stopped.values())):
                    row.note(f"{name} ja usado na Shopify por outro registro da Bagy")
            continue
        if stopped:
            for name in sorted(set(stopped.values())):
                row.note(f"{name} ja usado na Shopify por outro registro da Bagy")
        if row.shopify_id:
            duplicates, names = set(), set()
            for key, gids in hits[item.source_key]:
                for gid in gids:
                    if gid != row.shopify_id and gid not in claimed:
                        duplicates.add(gid)
                        names.add(key.name)
            if duplicates:
                row.candidates = sorted(duplicates)
                row.note("possivel duplicata na Shopify (mesmo " + ", ".join(sorted(names)) + ")")
    return rows


# --------------------------------------------------------------------------- #
# Entidades

def _same_value(a, b) -> bool:
    if a == b:
        return True
    for parse in (json.loads, float):
        try:
            return parse(str(a)) == parse(str(b))
        except (ValueError, TypeError):
            continue
    return False


def incompatible_definition(entity: str, definition: dict, record: dict) -> str | None:
    """Motivo pelo qual a definicao da loja nao serve para o payload (tipo e validacoes
    nao mudam no lugar na Shopify: so apagando e recriando). None se compativel."""
    if entity == "metafield_definition":
        if record.get("type") != definition.get("type"):
            return f"tipo {record.get('type')} na loja, {definition.get('type')} na transformacao"
        if "validations" not in record:
            return None  # snapshot antigo, sem validacoes
        wanted = {v["name"]: v["value"] for v in definition.get("validations") or []}
        have = {v.get("name"): v.get("value") for v in record.get("validations") or []}
        for name in sorted(set(wanted) | set(have)):
            if "bagy-ref:" in str(wanted.get(name, "")):
                continue
            if name not in wanted or name not in have or not _same_value(wanted[name], have[name]):
                return f"validacao {name} diferente"
        return None
    fields = record.get("fields") or []
    if fields and not isinstance(fields[0], dict):
        return None  # snapshot antigo, so com as chaves
    wanted = {field["key"]: field["type"] for field in definition.get("fieldDefinitions") or []}
    have = {field.get("key"): field.get("type") for field in fields}
    changed = sorted(key for key in set(wanted) | set(have) if wanted.get(key) != have.get(key))
    return ("campos diferentes: " + ", ".join(changed)) if changed else None


def diff_metafield_definitions(items, records) -> tuple:
    index = Index(records)
    for record in records:
        index.add("namespace.key", f"{record.get('owner_type')}|{record.get('namespace')}|{record.get('key')}",
                  record["id"])

    def definition(item):
        d = item.variables.get("definition") or {}
        return f"{d.get('ownerType')}|{d.get('namespace')}|{d.get('key')}"

    rows = match(items, [Key("namespace.key", definition)], index, upsert=False)
    for item in items:
        row = rows[item.source_key]
        if row.shopify_id:
            reason = incompatible_definition("metafield_definition", item.variables.get("definition") or {},
                                             index.by_gid[row.shopify_id])
            if reason:
                row.action = "conflict"
                row.note("definicao ja existe na Shopify incompativel (tipo ou validacao): se foi a carga que "
                         "criou, run_load.py --cleanup recria")
    return rows, index


def diff_metaobject_definitions(items, records) -> tuple:
    index = Index(records)
    for record in records:
        index.add("type", record.get("type"), record["id"])
    rows = match(items, [Key("type", lambda i: (i.variables.get("definition") or {}).get("type"))], index,
                 upsert=False)
    for item in items:
        row = rows[item.source_key]
        if row.shopify_id:
            reason = incompatible_definition("metaobject_definition", item.variables.get("definition") or {},
                                             index.by_gid[row.shopify_id])
            if reason:
                row.action = "conflict"
                row.note("metaobjeto ja existe na Shopify com campos diferentes: se foi a carga que criou, "
                         "run_load.py --cleanup recria")
    return rows, index


def filename_of(url) -> str | None:
    path = urlparse(unquote(str(url or ""))).path
    return path.rsplit("/", 1)[-1].lower() or None


def file_stem(name) -> str | None:
    """Nome sem extensao: a Shopify guarda como .webp o que o CDN da Bagy entrega em WebP
    (selo-vegan.jpg na origem vira selo-vegan.webp na loja)."""
    name = str(name or "").strip().lower()
    return (name.rsplit(".", 1)[0] if "." in name else name) or None


def diff_files(items, records) -> tuple:
    index = Index(records)
    for record in records:
        index.add("nome do arquivo", file_stem(record.get("filename")), record["id"])
    rows = match(items, [Key("nome do arquivo", lambda i: file_stem(filename_of(
        ((i.variables.get("files") or [{}])[0]).get("originalSource"))))], index, upsert=False)
    return rows, index


def diff_metaobjects(items, records) -> tuple:
    index = Index(records)
    for record in records:
        index.add("tipo+handle", f"{record.get('type')}|{record.get('handle')}", record["id"])
    rows = match(items, [Key("tipo+handle", lambda i: "{type}|{handle}".format(**(i.variables.get("handle") or {})))],
                 index, upsert=True)
    return rows, index


def diff_product_content(items, product_records) -> tuple:
    """metafieldsSet no produto: atualiza se o produto ja esta na loja."""
    index = Index(product_records)
    for record in product_records:
        index.add("bagy.product_id", record.get("product_id"), record["id"])

    def product_id(item):
        for value in item.variables.get("metafields") or []:
            owner = str(value.get("ownerId") or "")
            if owner.startswith("bagy-ref:product:"):
                return owner.rsplit(":", 1)[1]
        return None

    return match(items, [Key("bagy.product_id", product_id)], index, upsert=True), None


def diff_collections(items, records, weak: bool) -> tuple:
    index = Index(records)
    for record in records:
        index.add("bagy.category_id", record.get("category_id"), record["id"])
        index.add("handle", lower(record.get("handle")), record["id"])
        index.add("titulo", norm_text(record.get("title")), record["id"])

    def category(item):
        return metafield_value((item.variables.get("collection") or {}).get("metafields"), "bagy", "category_id")

    keys = [Key("bagy.category_id", category),
            Key("handle", lambda i: lower((i.variables.get("collection") or {}).get("handle")))]
    if weak:
        keys.append(Key("titulo", lambda i: norm_text((i.variables.get("collection") or {}).get("title")), False))
    rows = match(items, keys, index, upsert=False, identity=(category, lambda r: r.get("category_id")))
    return rows, index


def _product_input(item) -> dict:
    return item.variables.get("input") or {}


def _product_id(item):
    return metafield_value(_product_input(item).get("metafields"), "bagy", "product_id")


def pick_variant(record: dict, variation_id: str, item: Item) -> dict | None:
    variants = record.get("variants") or []
    for variant in variants:
        if str(variant.get("variation_id") or "") == str(variation_id):
            return variant
    payload_variants = _product_input(item).get("variants") or []
    skus = {norm_sku(v.get("sku")) for v in payload_variants
            if str(metafield_value(v.get("metafields"), "bagy", "variation_id")) == str(variation_id)}
    skus.discard(None)
    for variant in variants:
        if norm_sku(variant.get("sku")) in skus:
            return variant
    if len(variants) == 1 and len(payload_variants) == 1:
        return variants[0]
    return None


def diff_products(items, records, weak: bool) -> tuple:
    index = Index(records)
    for record in records:
        index.add("bagy.product_id", record.get("product_id"), record["id"])
        index.add("handle", lower(record.get("handle")), record["id"])
        index.add("titulo", norm_text(record.get("title")), record["id"])
        for variant in record.get("variants") or []:
            index.add("sku", norm_sku(variant.get("sku")), record["id"])

    keys = [Key("bagy.product_id", _product_id),
            Key("handle", lambda i: lower(_product_input(i).get("handle"))),
            Key("sku", lambda i: [norm_sku(v.get("sku")) for v in _product_input(i).get("variants") or []])]
    if weak:
        keys.append(Key("titulo", lambda i: norm_text(_product_input(i).get("title")), False))
    rows = match(items, keys, index, upsert=True, identity=(_product_id, lambda r: r.get("product_id")))

    for item in items:
        row = rows[item.source_key]
        if not row.shopify_id:
            continue
        record = index.by_gid[row.shopify_id]
        if lower(record.get("handle")) != lower(_product_input(item).get("handle")):
            row.note("handle diferente na Shopify: a carga deve usar identifier.id")
        for ref in item.provides:
            if ref.startswith("bagy-ref:product:"):
                row.refs[ref] = record["id"]
            elif ref.startswith("bagy-ref:variant:"):
                variant = pick_variant(record, ref.rsplit(":", 1)[1], item)
                if variant:
                    row.refs[ref] = variant["id"]
                else:
                    row.note("variante nao identificada no produto da Shopify")
    return rows, index


def _customer_id(item):
    for action in item.post_actions:
        if action.get("mutation") != "metafieldsSet":
            continue
        value = metafield_value((action.get("variables") or {}).get("metafields"), "bagy", "customer_id")
        if value:
            return value
    return None


def diff_customers(customer_items, lead_items, records) -> tuple:
    index = Index(records)
    for record in records:
        index.add("bagy.customer_id", record.get("customer_id"), record["id"])
        index.add("email", lower(record.get("email")), record["id"])
        index.add("telefone", only_digits(record.get("phone") or "") or None, record["id"])

    keys = [Key("bagy.customer_id", _customer_id),
            Key("email", lambda i: lower((i.variables.get("identifier") or {}).get("email")))]
    items = list(customer_items) + list(lead_items)
    rows = match(items, keys, index, upsert=True, identity=(_customer_id, lambda r: r.get("customer_id")))
    for item in items:
        row = rows[item.source_key]
        phone = only_digits((item.variables.get("input") or {}).get("phone") or "")
        if phone and row.action in ("create", "update"):
            if any(gid != row.shopify_id for gid in index.get("telefone", phone)):
                row.note("telefone ja usado por outro cliente na Shopify: enviar sem telefone")
    return rows, index


def diff_store_credits(items, customer_index, owner_rows: dict, provided: dict, currency: str) -> dict:
    rows = {}
    by_owner = defaultdict(list)
    for item in items:
        by_owner[item.variables.get("id")].append(item)

    for owner_ref, group in by_owner.items():
        owner_row = owner_rows.get(provided.get(owner_ref))
        total = sum((to_decimal(((i.variables.get("creditInput") or {}).get("creditAmount") or {}).get("amount"))
                     or Decimal("0.00") for i in group), Decimal("0.00"))
        balance = Decimal("0.00")
        if owner_row and owner_row.shopify_id and customer_index is not None:
            record = customer_index.by_gid.get(owner_row.shopify_id) or {}
            balance = sum((to_decimal(account.get("balance")) or Decimal("0.00")
                           for account in record.get("store_credit") or []
                           if account.get("currency") in (None, currency)), Decimal("0.00"))
        for item in group:
            row = rows[item.source_key] = Row("store_credit", item.source_key, payload_hash=item.hash)
            if owner_row is None:
                row.action = "unverified"
                row.note("cliente do credito nao esta entre os payloads prontos")
            elif owner_row.action in ("unverified", "conflict"):
                row.action = owner_row.action
                row.note(f"cliente do credito: {owner_row.action}")
            elif not owner_row.shopify_id or balance == 0:
                row.action = "create"
            elif balance == total:
                row.action, row.shopify_id, row.match_by = "skip", owner_row.shopify_id, "saldo igual"
                row.note("cliente ja tem na Shopify o mesmo saldo a migrar")
            else:
                row.action, row.shopify_id = "conflict", owner_row.shopify_id
                row.note("cliente ja tem saldo de credito diferente na Shopify")
    return rows


DISCOUNT_TYPES = {
    "discountCodeBasicCreate": "DiscountCodeBasic",
    "discountCodeFreeShippingCreate": "DiscountCodeFreeShipping",
    "discountCodeBxgyCreate": "DiscountCodeBxgy",
}


def diff_discounts(items, records) -> tuple:
    index = Index(records)
    for record in records:
        for code in record.get("codes") or []:
            index.add("codigo", lower(code), record["id"])
    rows = match(items, [Key("codigo", lambda i: lower(core(i).get("code")))], index, upsert=False)
    partial = any(int(record.get("codes_count") or 0) > len(record.get("codes") or []) for record in records)
    for item in items:
        row = rows[item.source_key]
        if row.shopify_id:
            expected = DISCOUNT_TYPES.get(item.mutation)
            if expected and index.by_gid[row.shopify_id].get("type") != expected:
                row.note("desconto com o mesmo codigo e de outro tipo na Shopify")
        elif row.action == "create" and partial:
            row.action = "unverified"
            row.note("ha descontos com mais de 10 codigos na Shopify: conferir o codigo no admin")
    return rows, index


def diff_by_handle(items, records, weak: bool) -> tuple:
    index = Index(records)
    for record in records:
        index.add("handle", lower(record.get("handle")), record["id"])
        index.add("titulo", norm_text(record.get("title")), record["id"])
    keys = [Key("handle", lambda i: lower(core(i).get("handle")))]
    if weak:
        keys.append(Key("titulo", lambda i: norm_text(core(i).get("title")), False))
    return match(items, keys, index, upsert=False), index


def diff_articles(items, records, blog_rows: dict, provided: dict, weak: bool) -> tuple:
    index = Index(records)
    for record in records:
        handle = lower(record.get("handle"))
        index.add("blog+handle", f"{record.get('blog_id')}|{handle}", record["id"])
        index.add("handle em outro blog", handle, record["id"])
        index.add("titulo", norm_text(record.get("title")), record["id"])

    def in_blog(item):
        article = item.variables.get("article") or {}
        blog_row = blog_rows.get(provided.get(article.get("blogId")))
        if blog_row and blog_row.shopify_id and blog_row.action in ("skip", "update"):
            return f"{blog_row.shopify_id}|{lower(article.get('handle'))}"
        return None

    keys = [Key("blog+handle", in_blog),
            Key("handle em outro blog", lambda i: lower((i.variables.get("article") or {}).get("handle")), False)]
    if weak:
        keys.append(Key("titulo", lambda i: norm_text((i.variables.get("article") or {}).get("title")), False))
    return match(items, keys, index, upsert=False), index


def diff_policies(items, records) -> dict:
    existing = {record.get("type"): record for record in records}
    rows = {}
    for item in items:
        policy = item.variables.get("shopPolicy") or {}
        row = rows[item.source_key] = Row("policy", item.source_key, payload_hash=item.hash)
        current = existing.get(policy.get("type"))
        body = norm_body(current.get("body")) if current else None
        if not body:
            continue
        row.shopify_id, row.match_by = current.get("id"), "tipo"
        if body == norm_body(policy.get("body")):
            row.action = "skip"
        else:
            row.action = "update"
            row.note("a politica ja tem texto na Shopify e sera substituida")
    return rows


def diff_redirects(items, records) -> tuple:
    index = Index(records)
    for record in records:
        index.add("path", norm_path(record.get("path")), record["id"])
    rows = match(items, [Key("path", lambda i: norm_path((i.variables.get("urlRedirect") or {}).get("path")))],
                 index, upsert=False)
    for item in items:
        row = rows[item.source_key]
        if row.shopify_id:
            target = norm_path(index.by_gid[row.shopify_id].get("target"))
            if target != norm_path((item.variables.get("urlRedirect") or {}).get("target")):
                row.action, row.candidates = "conflict", [row.shopify_id]
                row.note("o caminho ja redireciona para outro destino na Shopify")
    return rows, index


def diff_orders(items, records, *, all_orders_visible: bool) -> tuple:
    index = Index(records)
    for record in records:
        gid = record["id"]
        index.add("sourceIdentifier", lower(record.get("source_identifier")), gid)
        for tag in record.get("tags") or []:
            if (lower(tag) or "").startswith("bagy-id-"):
                index.add("tag bagy-id", lower(tag), gid)
        for token in record.get("tokens") or []:
            index.add("codigo do pedido", token, gid)
        index.add("data+total", order_stamp(record.get("processed_at"), record.get("total")), gid)

    def order(item):
        return item.variables.get("order") or {}

    def code(item):
        digits = only_digits(order(item).get("name") or "")
        return digits if len(digits) >= 10 else None

    def their_identity(record):
        value = lower(record.get("source_identifier"))
        return value if value and value.startswith("bagy-") else None

    keys = [
        Key("sourceIdentifier", lambda i: lower(order(i).get("sourceIdentifier"))),
        Key("tag bagy-id", lambda i: [lower(t) for t in order(i).get("tags") or []
                                      if (lower(t) or "").startswith("bagy-id-")]),
        Key("codigo do pedido", code),
        Key("data+total", lambda i: order_stamp(order(i).get("processedAt"), i.meta.get("total_shopify")), False),
    ]
    rows = match(items, keys, index, upsert=False,
                 identity=(lambda i: lower(order(i).get("sourceIdentifier")), their_identity))
    if not all_orders_visible:
        for row in rows.values():
            if row.action == "create":
                row.action = "unverified"
                row.note("sem read_all_orders a Shopify so mostra pedidos dos ultimos 60 dias")
    return rows, index


# --------------------------------------------------------------------------- #
# Orquestracao

def load_items(conn: sqlite3.Connection) -> dict:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(payloads)")}
    if not columns:
        raise DiffError("data/shopify.sqlite sem payloads: rode run_transform.py antes.")
    meta = "meta" if "meta" in columns else "NULL"
    grouped = defaultdict(list)
    for entity, key, mutation, variables, posts, provides, digest, meta_json in conn.execute(
            f"SELECT entity, source_key, mutation, variables, post_actions, provides, hash, {meta} "
            "FROM payloads WHERE status = 'ready' ORDER BY entity, position"):
        grouped[entity].append(Item(entity, key, mutation, json.loads(variables or "{}"),
                                    json.loads(posts or "[]"), json.loads(provides or "[]"), digest,
                                    json.loads(meta_json or "{}")))
    return grouped


def primary_location(records: list) -> str | None:
    active = [r for r in records if r.get("is_active") is not False]
    for test in (lambda r: r.get("is_primary"), lambda r: r.get("fulfills_online_orders"), lambda r: True):
        for record in active:
            if test(record):
                return record["id"]
    return None


def online_store(records: list) -> str | None:
    names = {"online store", "loja virtual", "loja online"}
    for record in records:
        if lower(record.get("name")) in names or lower(record.get("catalog_title")) in names:
            return record["id"]
    return None


def orphan_summary(resource: str, records: list, matched: set) -> dict:
    left = [record for record in records if record["id"] not in matched]
    summary = {"total": len(records), "sem_par": len(left)}
    if resource in ("products", "collections", "pages", "blogs", "menus", "articles"):
        handles = {lower(record.get("handle")) for record in records}
        summary["exemplos"] = sorted(str(record.get("handle")) for record in left)[:15]
        summary["handle_com_sufixo_numerico"] = sum(
            1 for record in records
            if re.search(r"-\d+$", str(record.get("handle") or ""))
            and re.sub(r"-\d+$", "", lower(record.get("handle")) or "") in handles)
    elif resource == "url_redirects":
        summary["exemplos"] = sorted(str(record.get("path")) for record in left)[:15]
    elif resource == "discounts":
        summary["exemplos"] = sorted(str(code) for record in left for code in record.get("codes") or [])[:15]
    elif resource == "metafield_definitions":
        summary["exemplos"] = sorted(f"{r.get('owner_type')} {r.get('namespace')}.{r.get('key')}" for r in left)[:15]
    elif resource == "orders":
        summary["app"] = dict(Counter(record.get("app") or "-" for record in left).most_common(5))
        summary["formato_do_nome"] = dict(Counter(re.sub(r"\d", "9", record.get("name") or "")
                                                  for record in left).most_common(5))
    elif resource == "customers":
        summary["tags"] = dict(Counter(tag for record in left for tag in record.get("tags") or []).most_common(8))
    return summary


def compute(grouped: dict, snapshot, *, currency: str = "BRL", match_by_title: bool = True) -> tuple:
    states = snapshot.states()

    def ready(resource) -> bool:
        return (states.get(resource) or {}).get("status") == "ok"

    cache = {}

    def records(resource) -> list:
        if resource not in cache:
            cache[resource] = snapshot.records(resource) if ready(resource) else []
        return cache[resource]

    rows = {}
    indexes = {}
    provided = {ref: (item.entity, item.source_key) for items in grouped.values() for item in items
                for ref in item.provides}

    def keep(entity_rows: dict) -> None:
        for row in entity_rows.values():
            rows[(row.entity, row.source_key)] = row

    def unverified(entity: str) -> None:
        resource = REQUIRES[entity]
        status = (states.get(resource) or {}).get("status")
        reason = {"forbidden": "sem permissao de leitura", "error": "erro na leitura",
                  "running": "leitura interrompida"}.get(status, "ainda nao lido")
        for item in grouped.get(entity, []):
            row = Row(entity, item.source_key, action="unverified", payload_hash=item.hash)
            row.note(f"snapshot de {resource}: {reason}")
            rows[(entity, item.source_key)] = row

    def run(entities: list, fn) -> None:
        if not any(grouped.get(entity) for entity in entities):
            return
        if ready(REQUIRES[entities[0]]):
            entity_rows, index = fn()
            keep(entity_rows)
            if index is not None:
                indexes[REQUIRES[entities[0]]] = index
        else:
            for entity in entities:
                unverified(entity)

    get = grouped.get
    run(["metaobject_definition"], lambda: diff_metaobject_definitions(get("metaobject_definition", []),
                                                                       records("metaobject_definitions")))
    run(["metafield_definition"], lambda: diff_metafield_definitions(get("metafield_definition", []),
                                                                     records("metafield_definitions")))
    run(["collection"], lambda: diff_collections(get("collection", []), records("collections"), match_by_title))
    run(["file"], lambda: diff_files(get("file", []), records("files")))
    run(["metaobject"], lambda: diff_metaobjects(get("metaobject", []), records("metaobjects")))
    run(["product"], lambda: diff_products(get("product", []), records("products"), match_by_title))
    run(["product_content"], lambda: diff_product_content(get("product_content", []), records("products")))
    run(["customer", "lead"], lambda: diff_customers(get("customer", []), get("lead", []), records("customers")))
    if get("store_credit"):
        owners = {key: row for key, row in rows.items() if key[0] in ("customer", "lead")}
        keep(diff_store_credits(get("store_credit", []), indexes.get("customers"), owners, provided, currency))
    run(["discount"], lambda: diff_discounts(get("discount", []), records("discounts")))
    run(["blog"], lambda: diff_by_handle(get("blog", []), records("blogs"), match_by_title))
    blog_rows = {key: row for key, row in rows.items() if key[0] == "blog"}
    run(["article"], lambda: diff_articles(get("article", []), records("articles"), blog_rows, provided,
                                           match_by_title))
    run(["policy"], lambda: (diff_policies(get("policy", []), records("policies")), None))
    run(["page"], lambda: diff_by_handle(get("page", []), records("pages"), match_by_title))
    run(["menu"], lambda: diff_by_handle(get("menu", []), records("menus"), match_by_title))
    run(["redirect"], lambda: diff_redirects(get("redirect", []), records("url_redirects")))

    scopes = set(snapshot.get_meta("access_scopes") or [])
    all_orders_visible = "read_all_orders" in scopes
    if not all_orders_visible:
        # Loja criada ha menos de 60 dias: todo pedido que ja existiu ainda aparece.
        created = parse_instant(((snapshot.records("shop") or [{}])[0]).get("created_at"))
        fetched = parse_instant((states.get("orders") or {}).get("finished_at"))
        all_orders_visible = bool(created and fetched and fetched - created < timedelta(days=ORDER_WINDOW_DAYS))
    run(["order"], lambda: diff_orders(get("order", []), records("orders"), all_orders_visible=all_orders_visible))

    items_by_key = {(item.entity, item.source_key): item for items in grouped.values() for item in items}
    refs = {}
    for key, row in rows.items():
        if row.action not in ("update", "skip") or not row.shopify_id:
            continue
        mapping = row.refs or {ref: row.shopify_id for ref in items_by_key[key].provides}
        for ref, gid in mapping.items():
            refs[ref] = (gid, row)
    location = primary_location(records("locations"))
    if location:
        refs["bagy-ref:location:default"] = (location, None)
    publication = online_store(records("publications"))
    if publication:
        refs["bagy-ref:publication:online_store"] = (publication, None)

    matched = defaultdict(set)
    for row in rows.values():
        resource = REQUIRES[row.entity]
        if row.shopify_id:
            matched[resource].add(row.shopify_id)
        matched[resource].update(row.candidates)
    orphans = {resource: orphan_summary(resource, records(resource), matched[resource])
               for resource in dict.fromkeys(REQUIRES.values())
               if ready(resource) and resource not in ("policies",)}

    flags = {"read_all_orders": "read_all_orders" in scopes, "all_orders_visible": all_orders_visible}
    return rows, refs, orphans, flags


def summarize(rows: dict) -> dict:
    summary = {}
    for entity in ENTITY_ORDER:
        entity_rows = [row for row in rows.values() if row.entity == entity]
        if not entity_rows:
            continue
        summary[entity] = {
            "total": len(entity_rows),
            "actions": {action: sum(1 for row in entity_rows if row.action == action) for action in ACTIONS},
            "match_by": dict(Counter(row.match_by for row in entity_rows
                                     if row.match_by and row.action in ("update", "skip")).most_common()),
            "weak": sum(1 for row in entity_rows if row.weak and row.action in ("update", "skip")),
            "notes": dict(Counter(note for row in entity_rows for note in row.notes).most_common()),
        }
    return summary


def save(conn: sqlite3.Connection, rows: dict, refs: dict, states: dict, diff_at: str) -> None:
    conn.executescript(SCHEMA)
    with conn:
        conn.execute("DELETE FROM existing")
        conn.execute("DELETE FROM ref_map")
        conn.executemany(
            "INSERT INTO existing (entity, source_key, action, shopify_id, match_by, weak_match, candidates, "
            "notes, payload_hash, snapshot_at, diff_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(row.entity, row.source_key, row.action, row.shopify_id, row.match_by, int(row.weak),
              json.dumps(row.candidates), json.dumps(row.notes, ensure_ascii=False), row.payload_hash,
              (states.get(REQUIRES[row.entity]) or {}).get("finished_at"), diff_at)
             for row in rows.values()])
        conn.executemany(
            "INSERT INTO ref_map (ref, shopify_id, entity, source_key, match_by) VALUES (?, ?, ?, ?, ?)",
            [(ref, gid, row.entity if row else "infra", row.source_key if row else None,
              row.match_by if row else None) for ref, (gid, row) in refs.items()])


def export(out_dir: Path, report: dict, rows: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_diff.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                        encoding="utf-8")
    with (out_dir / "diff_revisar.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows.values():
            if row.action in ("conflict", "unverified") or row.weak or row.notes:
                handle.write(json.dumps({
                    "entity": row.entity, "source_key": row.source_key, "action": row.action,
                    "shopify_id": row.shopify_id, "match_by": row.match_by, "weak_match": row.weak,
                    "candidates": row.candidates, "notes": row.notes,
                }, ensure_ascii=False) + "\n")


def run(transform_db: Path, snapshot, *, currency: str = "BRL", match_by_title: bool = True,
        out_dir: Path | None = None) -> dict:
    if not Path(transform_db).exists():
        raise DiffError(f"{transform_db} nao existe: rode run_transform.py antes.")
    diff_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    states = snapshot.states()
    conn = sqlite3.connect(str(transform_db))
    try:
        grouped = load_items(conn)
        rows, refs, orphans, flags = compute(grouped, snapshot, currency=currency, match_by_title=match_by_title)
        save(conn, rows, refs, states, diff_at)
    finally:
        conn.close()
    report = {
        "diff_at": diff_at,
        "shop": (snapshot.get_meta("shop") or {}).get("domain"),
        "snapshot": {name: {k: state.get(k) for k in ("status", "count", "finished_at", "error")}
                     for name, state in states.items()},
        "flags": flags,
        "entities": summarize(rows),
        "refs_resolvidas": len(refs),
        "orphans": orphans,
    }
    if out_dir:
        export(Path(out_dir), report, rows)
    return report
