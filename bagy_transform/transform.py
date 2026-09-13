"""CLI da transformacao: dados extraidos da Bagy -> payloads da Shopify.

So local: nada e enviado para a Shopify.

Uso (via run_transform.py):
    python run_transform.py                          # transforma tudo
    python run_transform.py --only pedidos           # um grupo ou entidade
    python run_transform.py --strict                 # codigo 1 se houver payload invalido
    python run_transform.py --reference-date 2026-10-01
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from .config import get_settings
from .entities import catalog, content, customers, manual, marketing, orders, product_content
from .mutations import DOCUMENTS
from .schema import SchemaValidator
from .source import Source
from .store import TransformStore

# Ordem = ordem de carga (dependencias primeiro).
ENTITIES = {
    "metaobject_definition": "Definicoes de metaobjeto",
    "metafield_definition": "Definicoes de metafield",
    "collection": "Colecoes (categorias)",
    "file": "Arquivos (imagens de selos e depoimentos)",
    "metaobject": "Metaobjetos (tabela, selos, especificacoes, depoimentos)",
    "product": "Produtos",
    "product_content": "Conteudo dos hotsites nos produtos",
    "customer": "Clientes",
    "lead": "Leads da newsletter",
    "store_credit": "Credito (cashback)",
    "discount": "Descontos",
    "blog": "Blog",
    "article": "Artigos",
    "policy": "Politicas",
    "page": "Paginas e hotsites",
    "menu": "Menus",
    "redirect": "Redirecionamentos",
    "order": "Pedidos",
}

GROUPS = {
    "catalogo": ["metaobject_definition", "metafield_definition", "collection", "file", "metaobject", "product",
                 "product_content"],
    "clientes": ["customer", "lead", "store_credit"],
    "marketing": ["discount"],
    "conteudo": ["blog", "article", "policy", "page", "menu", "redirect"],
    "pedidos": ["order"],
}


def select(only) -> list:
    if not only:
        return list(ENTITIES)
    chosen = set()
    for raw in only:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token in GROUPS:
                chosen.update(GROUPS[token])
            elif token in ENTITIES:
                chosen.add(token)
            else:
                raise ValueError(f"entidade ou grupo desconhecido: {token!r}")
    return [entity for entity in ENTITIES if entity in chosen]


def offset_delta(offset: str) -> timedelta:
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = offset.lstrip("+-").split(":")
    return sign * timedelta(hours=int(hours), minutes=int(minutes))


def reference_moment(value, src, settings) -> datetime:
    """'Hoje' da transformacao, no horario da loja.

    Padrao: o momento da extracao. Assim a transformacao e reprodutivel sobre os
    mesmos dados (vigencia de desconto, idade de pedido, expiracao de cashback).
    """
    if value:
        return datetime.strptime(value, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    extracted = src.extracted_at()
    if not extracted:
        return datetime.now()
    moment = datetime.fromisoformat(extracted)
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None) + offset_delta(settings.timezone_offset)
    return moment


def build_all(src, settings, reference) -> tuple:
    # O mapa de URLs vem primeiro: os links internos no HTML de produtos, posts,
    # politicas e paginas passam a apontar direto para o caminho novo.
    mapping = content.path_map(src, settings)
    payloads = []
    rich = product_content.build(src, settings)
    payloads += rich["metaobject_definition"]
    payloads += catalog.metafield_definitions(src)
    payloads += catalog.collections(src)
    payloads += rich["file"]
    payloads += rich["metaobject"]
    payloads += catalog.products(src, mapping)
    payloads += rich["product_content"]
    customer_payloads, used_phones = customers.customers(src, settings)
    payloads += customer_payloads
    payloads += customers.leads(src, settings, used_phones)
    credit_payloads, credit_stats = customers.store_credits(src, settings, reference)
    payloads += credit_payloads
    discount_payloads, manual_discounts = marketing.discounts(src, settings, reference)
    payloads += discount_payloads
    payloads += content.blog(src, settings)
    payloads += content.articles(src, settings, mapping)
    payloads += content.pages(src, settings, mapping)
    payloads += content.hotsite_pages(src, settings, mapping)
    payloads += content.menus(src, settings, mapping)
    payloads += content.redirects(src, settings, mapping)
    payloads += orders.orders(src, settings, reference)

    grouped = {entity: [] for entity in ENTITIES}
    for payload in payloads:
        grouped.setdefault(payload.entity, []).append(payload)

    credit_total = sum((Decimal(p.meta["amount"]) for p in credit_payloads if p.status != "skipped"),
                       Decimal("0.00"))
    context = {
        "reference": reference.strftime("%Y-%m-%d %H:%M"),
        "credit_stats": dict(credit_stats),
        "credit_total": str(credit_total),
        "manual_discounts": manual_discounts,
        "path_map_size": len(mapping),
        "links_rewritten": sum(payload.meta.get("links_rewritten", 0) for payload in payloads),
    }
    return grouped, context


def validate(validator, payload) -> None:
    errors, notices = validator.validate(payload.mutation, payload.variables)
    for index, action in enumerate(payload.post_actions):
        if not action.get("mutation"):
            continue
        action_errors, action_notices = validator.validate(action["mutation"], action.get("variables") or {})
        errors += [f"pos-acao {index} {action['mutation']} {message}" for message in action_errors]
        notices += [f"pos-acao {index} {action['mutation']} {message}" for message in action_notices]
    payload.schema_errors, payload.schema_notices = errors, notices
    if errors:
        payload.status = "invalid"


def redact(message: str) -> str:
    """Tira o valor (pode ser dado pessoal) e os indices, para agrupar mensagens iguais."""
    return re.sub(r"\[\d+\]", "[]", message.split(" | valor:")[0])


def summarize(grouped: dict, wanted: list) -> dict:
    summary = {}
    for entity in wanted:
        items = grouped.get(entity, [])
        statuses = Counter(payload.status for payload in items)
        summary[entity] = {
            "ready": statuses["ready"],
            "skipped": statuses["skipped"],
            "invalid": statuses["invalid"],
            "with_warnings": sum(1 for payload in items if payload.warnings),
            "mutations": sorted({payload.mutation for payload in items if payload.mutation}),
            "warnings": dict(Counter(w for p in items for w in p.warnings).most_common()),
            "skip_reasons": dict(Counter(p.skip_reason for p in items if p.skip_reason).most_common()),
            "schema_errors": dict(Counter(redact(e) for p in items for e in p.schema_errors).most_common(25)),
            "schema_notices": dict(Counter(redact(n) for p in items for n in p.schema_notices).most_common(10)),
        }
    return summary


def order_totals(items: list) -> dict:
    considered = [payload for payload in items if payload.status != "skipped" and payload.meta]
    bagy = sum((Decimal(p.meta["total_bagy"]) for p in considered), Decimal("0.00"))
    shopify = sum((Decimal(p.meta["total_shopify"]) for p in considered), Decimal("0.00"))
    return {
        "orders": len(considered),
        "total_bagy": str(bagy),
        "total_shopify": str(shopify),
        "divergent": sum(1 for p in considered if p.meta["total_bagy"] != p.meta["total_shopify"]),
    }


def export(grouped: dict, wanted: list, settings, summary: dict, context: dict, checklist_md: str) -> list:
    out_dir = settings.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    used = set()
    for entity in wanted:
        with (out_dir / f"{entity}.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for payload in grouped.get(entity, []):
                if payload.status == "skipped":
                    continue
                used.add(payload.mutation)
                used.update(action["mutation"] for action in payload.post_actions if action.get("mutation"))
                handle.write(json.dumps({
                    "source_key": payload.source_key,
                    "status": payload.status,
                    "mutation": payload.mutation,
                    "variables": payload.variables,
                    "post_actions": payload.post_actions,
                    "provides": payload.provides,
                    "depends_on": payload.depends_on,
                    "warnings": payload.warnings,
                }, ensure_ascii=False) + "\n")

    missing = sorted(mutation for mutation in used if mutation not in DOCUMENTS)
    documents = [DOCUMENTS[mutation] for mutation in sorted(used) if mutation in DOCUMENTS]
    (out_dir / "mutations.graphql").write_text("\n\n".join(documents) + "\n", encoding="utf-8")
    report = {"reference": context["reference"], "entities": summary,
              "context": {k: v for k, v in context.items() if k != "reference"},
              "missing_documents": missing}
    (out_dir / "_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                          encoding="utf-8")
    (out_dir / "checklist_manual.md").write_text(checklist_md, encoding="utf-8")
    return missing


def print_report(summary: dict, wanted: list, context: dict, elapsed: float, settings,
                 exported: bool, missing: list) -> None:
    print("\n" + "=" * 100)
    print(f"{'Entidade':<22} {'Prontos':>8} {'Pulados':>8} {'Invalidos':>10} {'Com aviso':>10}  Mutation")
    print("-" * 100)
    for entity in wanted:
        s = summary[entity]
        print(f"{entity:<22} {s['ready']:>8} {s['skipped']:>8} {s['invalid']:>10} "
              f"{s['with_warnings']:>10}  {', '.join(s['mutations'])}")
    print("-" * 100)
    ready, skipped_count, invalid = (sum(summary[e][k] for e in wanted) for k in ("ready", "skipped", "invalid"))
    print(f"{'total':<22} {ready:>8} {skipped_count:>8} {invalid:>10}")

    for entity in wanted:
        s = summary[entity]
        if not (s["skip_reasons"] or s["warnings"] or s["schema_errors"] or s["schema_notices"]):
            continue
        print(f"\n[{entity}] {ENTITIES[entity]}")
        for reason, count in s["skip_reasons"].items():
            print(f"   pulado   {count:>5}x  {reason}")
        for warning, count in list(s["warnings"].items())[:8]:
            print(f"   aviso    {count:>5}x  {warning}")
        for error, count in list(s["schema_errors"].items())[:8]:
            print(f"   SCHEMA   {count:>5}x  {error}")
        for notice, count in list(s["schema_notices"].items())[:4]:
            print(f"   deprec.  {count:>5}x  {notice}")

    totals = context.get("order_totals")
    if totals:
        print(f"\nPedidos: {totals['orders']} | total Bagy R$ {Decimal(totals['total_bagy']):,.2f} | "
              f"total a enviar R$ {Decimal(totals['total_shopify']):,.2f} | divergentes: {totals['divergent']}")
    if context.get("credit_stats"):
        print("Cashback: " + ", ".join(f"{k}: {v}" for k, v in context["credit_stats"].items())
              + f" | saldo migrado R$ {context['credit_total']}")
    if missing:
        print(f"\nATENCAO: mutations sem documento GraphQL: {', '.join(missing)}")
    print(f"\nTempo: {elapsed:.1f}s | referencia: {context['reference']} (horario da loja)")
    print(f"Banco   : {settings.out_db}")
    if exported:
        print(f"Arquivos: {settings.out_dir}  (um .jsonl por entidade, mutations.graphql, "
              "_report.json, checklist_manual.md)")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_transform",
        description="Transforma os dados extraidos da Bagy em payloads da Shopify (sem enviar nada).")
    parser.add_argument("--only", action="append", metavar="ENTIDADE|GRUPO",
                        help="so estas (virgula ou repetido). Grupos: " + ", ".join(GROUPS))
    parser.add_argument("--reference-date", metavar="AAAA-MM-DD",
                        help="data usada como 'hoje' para vigencia, idade de pedido e expiracao "
                             "de cashback (padrao: momento da extracao)")
    parser.add_argument("--strict", action="store_true",
                        help="sai com codigo 1 se algum payload for invalido no schema")
    parser.add_argument("--no-export", action="store_true",
                        help="nao grava os arquivos em data/shopify")
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        wanted = select(args.only)
    except ValueError as exc:
        print(f"ERRO: {exc}")
        print(f"  Grupos   : {', '.join(GROUPS)}")
        print(f"  Entidades: {', '.join(ENTITIES)}")
        return 2

    try:
        src = Source(settings.source_db)
        validator = SchemaValidator(settings.schema_path)
    except FileNotFoundError as exc:
        print(f"ERRO: {exc}")
        return 2

    started = time.monotonic()
    try:
        reference = reference_moment(args.reference_date, src, settings)
        print("=" * 72)
        print("  TRANSFORMACAO BAGY -> SHOPIFY  (local - nada vai para a Shopify)")
        print("=" * 72)
        print(f"Origem    : {settings.source_db}")
        print(f"Schema    : Admin API {settings.api_version}")
        print(f"Referencia: {reference:%Y-%m-%d %H:%M} (horario da loja)")
        print(f"Entidades : {len(wanted)} de {len(ENTITIES)}")

        grouped, context = build_all(src, settings, reference)
        for entity in wanted:
            for payload in grouped.get(entity, []):
                if payload.status == "ready":
                    validate(validator, payload)
        summary = summarize(grouped, wanted)
        if "order" in wanted:
            context["order_totals"] = order_totals(grouped.get("order", []))

        store = TransformStore(settings.out_db)
        try:
            run_id = store.start_run(context["reference"], wanted)
            for entity in wanted:
                store.replace(entity, grouped.get(entity, []), run_id)
            store.finish_run(run_id, summary)
        finally:
            store.close()

        missing = []
        if not args.no_export:
            missing = export(grouped, wanted, settings, summary, context,
                             manual.checklist(src, settings, grouped, context))
        print_report(summary, wanted, context, time.monotonic() - started, settings,
                     not args.no_export, missing)
    finally:
        src.close()

    invalid = sum(summary[entity]["invalid"] for entity in wanted)
    return 1 if (args.strict and invalid) else 0
