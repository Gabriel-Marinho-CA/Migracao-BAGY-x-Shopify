"""Prova onde os pedidos importados foram parar na loja.

Util quando a lista de pedidos do admin parece nao ter mudado: pedidos com
processedAt no passado entram no fim da listagem, nao no topo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError

COUNTS = [
    ("total de pedidos na loja", None),
    ("importados da Bagy (tag)", "tag:bagy-import"),
    ("criados hoje", "created_at:>=2026-08-27"),
    ("com data de 2020", "processed_at:>=2020-01-01 AND processed_at:<=2020-12-31"),
    ("com data de 2021", "processed_at:>=2021-01-01 AND processed_at:<=2021-12-31"),
    ("com data de 2025", "processed_at:>=2025-01-01 AND processed_at:<=2025-12-31"),
    ("pagos (tag Bagy)", "tag:bagy-import AND financial_status:paid"),
    ("pendentes (tag Bagy)", "tag:bagy-import AND financial_status:pending"),
    ("nao despachados (tag Bagy)", "tag:bagy-import AND fulfillment_status:unfulfilled"),
]

BAGY_IDS = ["5", "15", "1167", "1168", "1172", "1180", "1185", "1190"]


def main() -> None:
    settings = get_settings()
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    for label, query in COUNTS:
        try:
            print(f"  {label:<32} {client.order_count(query)}")
        except ShopifyError as exc:
            print(f"  {label:<32} erro: {str(exc)[:80]}")

    print("\n  Por pedido da Bagy:")
    for bagy_id in BAGY_IDS:
        try:
            found = client.order_count(f"tag:bagy-id-{bagy_id}")
            print(f"    Bagy #{bagy_id:<6} {'ENCONTRADO' if found else 'ausente':<12} ({found})")
        except ShopifyError as exc:
            print(f"    Bagy #{bagy_id:<6} erro: {str(exc)[:60]}")

    store = settings.store_domain.replace(".myshopify.com", "")
    print(f"\n  Ver no admin:")
    print(f"    https://admin.shopify.com/store/{store}/orders?query=tag%3Abagy-import")


if __name__ == "__main__":
    main()
