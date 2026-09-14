"""Aponta produtos e paginas para os templates gerados dos hotsites (templateSuffix).

Produto ou pagina que ainda nao existe na loja fica para a proxima rodada.
"""

from __future__ import annotations

from collections import Counter

PRODUCT = "query($handle: String!) { productByIdentifier(identifier: {handle: $handle}) { id templateSuffix } }"
PRODUCT_UPDATE = """mutation($product: ProductUpdateInput!) { productUpdate(product: $product) {
  product { id templateSuffix } userErrors { field message } } }"""
PAGE = "query($q: String!) { pages(first: 5, query: $q) { nodes { id handle templateSuffix } } }"
PAGE_UPDATE = """mutation($id: ID!, $page: PageUpdateInput!) { pageUpdate(id: $id, page: $page) {
  page { id templateSuffix } userErrors { field message } } }"""


def run(client, assignments: dict, *, dry_run: bool = False, log=print) -> Counter:
    stats = Counter()
    for handle, suffix in sorted(assignments.get("products", {}).items()):
        product = client.execute(PRODUCT, {"handle": handle})["productByIdentifier"]
        stats.update(_apply(client, "produto", handle, suffix, product, dry_run, log,
                            lambda gid: client.execute(PRODUCT_UPDATE, {"product": {"id": gid, "templateSuffix": suffix}})["productUpdate"]))
    for handle, suffix in sorted(assignments.get("pages", {}).items()):
        nodes = client.execute(PAGE, {"q": f"handle:{handle}"})["pages"]["nodes"]
        page = next((node for node in nodes if node["handle"] == handle), None)
        stats.update(_apply(client, "pagina", handle, suffix, page, dry_run, log,
                            lambda gid: client.execute(PAGE_UPDATE, {"id": gid, "page": {"templateSuffix": suffix}})["pageUpdate"]))
    return stats


def _apply(client, kind, handle, suffix, node, dry_run, log, update) -> Counter:
    if not node:
        return Counter({f"{kind} ainda nao existe": 1})
    if node.get("templateSuffix") == suffix:
        return Counter({"ja apontados": 1})
    if dry_run:
        log(f"  [dry-run] {kind} {handle} -> template {suffix}")
        return Counter({"a apontar": 1})
    result = update(node["id"])
    if result["userErrors"]:
        log(f"  erro no {kind} {handle}: {result['userErrors']}")
        return Counter({"erros": 1})
    log(f"  {kind} {handle} -> template {suffix}")
    return Counter({"apontados": 1})
