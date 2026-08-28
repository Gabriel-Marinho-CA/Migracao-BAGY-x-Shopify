"""Verifica se da para dar fulfillment nos pedidos JA criados.

Sem acesso ao objeto Order (Protected Customer Data), a duvida e se ainda
existe algum caminho ate os fulfillment orders. Testa as rotas possiveis.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError

PROBES = [
    ("orders por tag",
     '{ orders(first: 3, query: "tag:bagy-id-15") { nodes { id name } } }'),
    ("assignedFulfillmentOrders",
     "{ assignedFulfillmentOrders(first: 3) { nodes { id status } } }"),
    ("fulfillmentOrders (raiz)",
     "{ fulfillmentOrders(first: 3) { nodes { id status } } }"),
    ("locations + fulfillment",
     "{ locations(first: 2) { nodes { id name isActive fulfillsOnlineOrders } } }"),
]


def main() -> None:
    settings = get_settings()
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    for label, query in PROBES:
        try:
            print(f"[ok]      {label}: {client.execute(query)}")
        except ShopifyError as exc:
            print(f"[NEGADO]  {label}: {str(exc)[:190]}")


if __name__ == "__main__":
    main()
