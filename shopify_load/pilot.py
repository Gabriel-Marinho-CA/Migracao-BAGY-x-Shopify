"""Selecao do piloto: poucos pedidos que cobrem os casos diferentes, e so as
dependencias deles (clientes, produtos, colecoes, paginas, definicoes).

A selecao fica salva em data/shopify/piloto.json: rodar o piloto de novo
continua o MESMO conjunto, em vez de sortear outro.
"""

from __future__ import annotations

import json
from pathlib import Path

from bagy_transform.common import REF_RE

INFRA = ("bagy-ref:location:", "bagy-ref:publication:")


def features(order) -> set:
    """O que o pedido exercita na carga."""
    data = order.variables.get("order") or {}
    posts = {action.get("mutation") or action.get("action") for action in order.post_actions}
    fulfillment = data.get("fulfillment") or {}
    found = {f"pagamento {data.get('financialStatus')}"}
    if fulfillment:
        found.add("enviado com rastreio" if fulfillment.get("trackingNumber") else "enviado sem rastreio")
    if "orderCancel" in posts:
        found.add("cancelado")
    if data.get("discountCode"):
        found.add("com desconto")
    if "orderUpdate" in posts:
        found.add("com CPF/CNPJ")
    if len(data.get("lineItems") or []) > 1:
        found.add("varios itens")
    if data.get("closedAt"):
        found.add("arquivado")
    return found


def refs_in(item) -> set:
    return set(REF_RE.findall(json.dumps([item.variables, item.post_actions], ensure_ascii=False)))


def select(grouped: dict, existing: dict, max_orders: int = 10) -> dict:
    items = {(item.entity, item.source_key): item for entries in grouped.values() for item in entries}
    provider = {ref: (item.entity, item.source_key) for item in items.values() for ref in item.provides}

    def loadable(key) -> bool:
        return (existing.get(key) or {}).get("action") in ("create", "update")

    candidates = []
    for order in grouped.get("order", []):
        if not loadable(("order", order.source_key)):
            continue
        refs = {ref for ref in refs_in(order) if not ref.startswith(INFRA)}
        if any(ref not in provider for ref in refs):
            continue
        owners = {provider[ref] for ref in refs}
        if not any(owner[0] == "customer" for owner in owners) or not all(loadable(owner) for owner in owners):
            continue
        products = {owner for owner in owners if owner[0] == "product"}
        candidates.append((order, features(order), products, owners))

    # Cobertura gulosa: a cada passo, o pedido que exercita mais casos ainda nao
    # cobertos; no empate, o que traz menos produtos novos.
    chosen, covered, products = [], set(), set()
    while candidates and len(chosen) < max_orders:
        best = max(candidates, key=lambda c: (len(c[1] - covered), -len(c[2] - products), c[0].source_key))
        if not best[1] - covered:
            break
        chosen.append(best)
        covered |= best[1]
        products |= best[2]
        candidates.remove(best)

    selection = {("order", c[0].source_key) for c in chosen}
    for _, _, _, owners in chosen:
        selection |= owners
    # Dependencias dos produtos (colecoes, pagina de conteudo) e dos clientes.
    for key in list(selection):
        for ref in refs_in(items[key]):
            if ref in provider and not ref.startswith(INFRA):
                selection.add(provider[ref])
    selection |= {("metafield_definition", item.source_key) for item in grouped.get("metafield_definition", [])
                  if loadable(("metafield_definition", item.source_key))}
    customers = {ref for key in selection if key[0] == "customer" for ref in items[key].provides}
    selection |= {("store_credit", item.source_key) for item in grouped.get("store_credit", [])
                  if item.variables.get("id") in customers and loadable(("store_credit", item.source_key))}

    result = {}
    for entity, key in sorted(selection):
        result.setdefault(entity, []).append(key)
    result["_casos_cobertos"] = sorted(covered)
    return result


def load_or_select(path: Path, grouped: dict, existing: dict, max_orders: int) -> tuple:
    """(selecao, nova?) - reaproveita o piloto salvo se existir."""
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), False
    selection = select(grouped, existing, max_orders)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    return selection, True
