"""Remove da loja o que a carga criou e nao serve mais.

Dois casos, sempre restritos ao que a PROPRIA carga criou (load_items com
status done/partial):

1. Retirado: o payload sumiu da transformacao ou virou "pulado".
   Ex.: paginas dos hotsites de produto, a definicao custom.conteudo_pagina.
2. Incompativel: definicao de metafield ou de metaobjeto cujo tipo, campos ou
   validacoes mudaram na transformacao. A Shopify nao muda isso no lugar, entao
   apaga para a proxima carga recriar (com os valores, que a carga regrava).
   Compara com o snapshot da loja: rode run_shopify_snapshot.py antes.

Metaobjetos de uma definicao que vai ser apagada somem junto com ela (sem
chamada propria). Apagar definicao de metafield leva os valores nos produtos.
"""

from __future__ import annotations

import json
from collections import Counter

from bagy2shopify.shopify_client import ShopifyError, ShopifyUserError
from shopify_snapshot.diff import incompatible_definition

PAGE_DELETE = """mutation PageDelete($id: ID!) {
  pageDelete(id: $id) {
    deletedPageId
    userErrors {
      field
      message
    }
  }
}"""

DEFINITION_DELETE = """mutation MetafieldDefinitionDelete($id: ID!) {
  metafieldDefinitionDelete(id: $id, deleteAllAssociatedMetafields: true) {
    deletedDefinitionId
    userErrors {
      field
      message
      code
    }
  }
}"""

METAOBJECT_DELETE = """mutation MetaobjectDelete($id: ID!) {
  metaobjectDelete(id: $id) {
    deletedId
    userErrors {
      field
      message
      code
    }
  }
}"""

METAOBJECT_DEFINITION_DELETE = """mutation MetaobjectDefinitionDelete($id: ID!) {
  metaobjectDefinitionDelete(id: $id) {
    deletedId
    userErrors {
      field
      message
      code
    }
  }
}"""

REMOVABLE = {
    "metafield_definition": ("metafieldDefinitionDelete", DEFINITION_DELETE),
    "page": ("pageDelete", PAGE_DELETE),
    "metaobject": ("metaobjectDelete", METAOBJECT_DELETE),
    "metaobject_definition": ("metaobjectDefinitionDelete", METAOBJECT_DEFINITION_DELETE),
}
# Campos do produto antes (podem validar contra o metaobjeto); definicao do metaobjeto por ultimo.
ORDER = ("metafield_definition", "page", "metaobject", "metaobject_definition")
SNAPSHOT_RESOURCES = {"metafield_definition": "metafield_definitions", "metaobject_definition": "metaobject_definitions"}
GONE_HINTS = ("not found", "not exist", "does not exist", "nao encontrad", "não encontrad")


def _type(entity: str, key: str) -> str:
    return key.split(":")[1] if ":" in key else ""


def invalidate_values(store, definition_key: str) -> int:
    """Apagar a definicao leva os valores: quem gravou aquele campo com metafieldsSet volta a pendente.

    definition_key no formato da transformacao: "product.custom.selos". Sem isso,
    um conteudo de produto com payload igual (ex.: so a tabela nutricional) seria
    pulado como "ja carregado" e o produto ficaria sem o campo.
    """
    parts = definition_key.split(".", 2)
    if len(parts) != 3:
        return 0
    _, namespace, key = parts
    items = store.items()
    count = 0
    for entity, source_key, variables in store.conn.execute(
            "SELECT entity, source_key, variables FROM payloads WHERE mutation = 'metafieldsSet'").fetchall():
        if (items.get((entity, source_key)) or {}).get("status") != "done":
            continue
        metafields = json.loads(variables or "{}").get("metafields") or []
        if any(m.get("namespace") == namespace and m.get("key") == key for m in metafields):
            store.mark(entity, source_key, "removed",
                       error=f"valores apagados com a definicao {definition_key} - a carga regrava")
            count += 1
    return count


def plan(store, snapshot=None) -> list:
    """O que apagar, na ordem certa: [{entity, key, gid, reason, with_definition}]."""
    payloads = {(entity, key): (status, json.loads(variables or "{}")) for entity, key, status, variables in
                store.conn.execute("SELECT entity, source_key, status, variables FROM payloads")}
    records = {entity: {record["id"]: record for record in snapshot.records(resource)}
               for entity, resource in SNAPSHOT_RESOURCES.items()} if snapshot is not None else {}
    created = {key: row for key, row in store.items().items()
               if key[0] in REMOVABLE and row.get("status") in ("done", "partial") and row.get("shopify_id")}

    found = {}
    for (entity, key), row in created.items():
        status, variables = payloads.get((entity, key), (None, {}))
        if status in (None, "skipped"):
            found[(entity, key)] = "nao e mais gerado pela transformacao"
            continue
        record = records.get(entity, {}).get(row["shopify_id"])
        if record:
            reason = incompatible_definition(entity, variables.get("definition") or {}, record)
            if reason:
                found[(entity, key)] = f"incompativel ({reason}) - a carga recria"

    doomed = {_type(entity, key) for entity, key in found if entity == "metaobject_definition"}
    for (entity, key) in created:
        if entity == "metaobject" and _type(entity, key) in doomed and (entity, key) not in found:
            found[(entity, key)] = "a definicao do metaobjeto vai ser recriada - a carga recria"
        elif entity == "metafield_definition" and (entity, key) not in found:
            # O campo valida pelo ID da definicao do metaobjeto: recriada a definicao,
            # o ID muda e o campo tem que ser recriado junto.
            validations = ((payloads.get((entity, key), (None, {}))[1].get("definition") or {}).get("validations")
                           or [])
            targets = {str(v.get("value") or "").rsplit(":", 1)[-1] for v in validations
                       if str(v.get("value") or "").startswith("bagy-ref:metaobject_definition:")}
            if targets & doomed:
                found[(entity, key)] = "valida contra metaobjeto que vai ser recriado - a carga recria"

    return [{"entity": entity, "key": key, "gid": created[(entity, key)]["shopify_id"], "reason": reason,
             "with_definition": entity == "metaobject" and _type(entity, key) in doomed}
            for entity in ORDER for (e, key), reason in sorted(found.items()) if e == entity]


def retired(store, snapshot=None) -> list:
    return [(item["entity"], item["key"], item["gid"]) for item in plan(store, snapshot)]


def cleanup(client, store, *, snapshot=None, dry_run: bool = False, errors=None, log=print) -> Counter:
    results = Counter()
    items = plan(store, snapshot)
    log(f"\n[limpeza] {len(items)} item(ns) criados pela carga que nao servem mais")
    deferred, failed_types = [], set()
    for item in items:
        label = f"{item['entity']:<22} {item['key']}"
        if dry_run:
            results["apagaria"] += 1
            log(f"  [apagaria    ] {label}  ({item['reason']})")
            continue
        if item["with_definition"]:
            deferred.append(item)
            continue
        root, document = REMOVABLE[item["entity"]]
        try:
            data = client.execute(document, {"id": item["gid"]})
        except ShopifyError as exc:
            if exc.status == 401:
                raise
            results["falhou"] += 1
            if item["entity"] == "metaobject_definition":
                failed_types.add(_type(item["entity"], item["key"]))
            if errors is not None:
                errors.record(script="load", stage=f"cleanup-{root}", bagy_id=item["key"], message=str(exc)[:300],
                              error=exc)
            log(f"  [falhou      ] {label}  {' '.join(str(exc).split())[:200]}")
            continue
        user_errors = (data.get(root) or {}).get("userErrors") or []
        gone = user_errors and all(any(hint in (e.get("message") or "").lower() for hint in GONE_HINTS)
                                   for e in user_errors)
        if user_errors and not gone:
            exc = ShopifyUserError(user_errors, request_id=client.last_request_id)
            results["falhou"] += 1
            if item["entity"] == "metaobject_definition":
                failed_types.add(_type(item["entity"], item["key"]))
            if errors is not None:
                errors.record(script="load", stage=f"cleanup-{root}", bagy_id=item["key"], message=str(exc)[:300],
                              error=exc)
            log(f"  [falhou      ] {label}  {str(exc)[:200]}")
            continue
        store.mark(item["entity"], item["key"], "removed", error=None)
        store.forget_refs(item["entity"], item["key"])
        results["apagado"] += 1
        log(f"  [{'ja nao existia' if gone else 'apagado':<12}] {label}")
        if item["entity"] == "metafield_definition":
            pending = invalidate_values(store, item["key"])
            if pending:
                results["valores a regravar"] += pending
                log(f"  [a regravar  ] {pending} item(ns) que gravavam {item['key']} voltam a pendente")

    for item in deferred:
        if _type(item["entity"], item["key"]) in failed_types:
            continue
        store.mark(item["entity"], item["key"], "removed", error=None)
        store.forget_refs(item["entity"], item["key"])
        results["apagado junto"] += 1
        log(f"  [junto c/ def] {item['entity']:<22} {item['key']}")
    return results
