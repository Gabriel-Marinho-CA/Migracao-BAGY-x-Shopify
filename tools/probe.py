"""Sonda o que o token atual consegue ler/escrever na loja.

Uso: python tools/probe.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError

QUERIES = [
    ("shop (basico)", "{ shop { name currencyCode } }"),
    ("ordersCount total", "{ ordersCount { count } }"),
    ("ordersCount tag bagy-import",
     '{ ordersCount(query: "tag:bagy-import") { count } }'),
    ("ordersCount tag bagy-id-5",
     '{ ordersCount(query: "tag:bagy-id-5") { count } }'),
    ("orders (lista)", "{ orders(first: 5) { nodes { id name createdAt } } }"),
    ("customers (lista)", "{ customers(first: 3) { nodes { id } } }"),
    ("locations", "{ locations(first: 3) { nodes { id name } } }"),
]


def main() -> None:
    settings = get_settings()
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    for label, query in QUERIES:
        try:
            data = client.execute(query)
            print(f"[ok]      {label}: {data}")
        except ShopifyError as exc:
            print(f"[NEGADO]  {label}: {str(exc)[:200]}")


if __name__ == "__main__":
    main()
