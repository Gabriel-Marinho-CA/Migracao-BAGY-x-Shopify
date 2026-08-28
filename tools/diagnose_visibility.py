"""Descobre por que um pedido nao aparece na aba de Pedidos do admin.

Um pedido pode existir (e contar em relatorios) mas sumir da listagem por
arquivamento, cancelamento, marcacao de teste ou canal de venda. Este script
le os campos que controlam isso.

Uso: python tools/diagnose_visibility.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError

QUERY = """
query DiagnoseVisibility {
  orders(first: 20, query: "tag:bagy-import", sortKey: CREATED_AT, reverse: true) {
    nodes {
      id
      name
      createdAt
      processedAt
      closed
      closedAt
      cancelledAt
      cancelReason
      test
      confirmed
      displayFinancialStatus
      displayFulfillmentStatus
      sourceIdentifier
      app {
        name
      }
      publication {
        name
      }
      tags
    }
  }
}
"""


def main() -> int:
    settings = get_settings()
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    try:
        data = client.execute(QUERY)
    except ShopifyError as exc:
        print(f"ERRO: {exc}")
        return 1

    nodes = (data.get("orders") or {}).get("nodes") or []
    if not nodes:
        print("Nenhum pedido com a tag bagy-import encontrado.")
        return 1

    print(f"{len(nodes)} pedido(s) com a tag bagy-import\n")

    closed, cancelled, test_orders, unconfirmed = [], [], [], []

    for node in nodes:
        print(f"{node['name']}  (sourceIdentifier: {node.get('sourceIdentifier')})")
        print(f"  criado em   : {node['createdAt']}")
        print(f"  processado  : {node['processedAt']}")
        print(f"  arquivado   : {node['closed']}"
              f"{' em ' + node['closedAt'] if node.get('closedAt') else ''}")
        print(f"  cancelado   : {node.get('cancelledAt') or 'nao'}"
              f"{' (' + str(node['cancelReason']) + ')' if node.get('cancelReason') else ''}")
        print(f"  teste       : {node['test']}")
        print(f"  confirmado  : {node.get('confirmed')}")
        print(f"  pagamento   : {node['displayFinancialStatus']}")
        print(f"  fulfillment : {node['displayFulfillmentStatus']}")
        print(f"  app         : {(node.get('app') or {}).get('name', '-')}")
        print(f"  publicacao  : {(node.get('publication') or {}).get('name', '-')}")
        print()

        if node["closed"]:
            closed.append(node["name"])
        if node.get("cancelledAt"):
            cancelled.append(node["name"])
        if node["test"]:
            test_orders.append(node["name"])
        if node.get("confirmed") is False:
            unconfirmed.append(node["name"])

    print("-" * 72)
    print("Motivos possiveis de sumir da aba de Pedidos:\n")

    if closed:
        print(f"  ARQUIVADOS ({len(closed)}): {', '.join(closed)}")
        print("    -> A aba de Pedidos abre no filtro que esconde arquivados.")
        print("       No admin, use o filtro 'Arquivado' ou a busca por tag.")
    if cancelled:
        print(f"  CANCELADOS ({len(cancelled)}): {', '.join(cancelled)}")
    if test_orders:
        print(f"  MARCADOS COMO TESTE ({len(test_orders)}): {', '.join(test_orders)}")
    if unconfirmed:
        print(f"  NAO CONFIRMADOS ({len(unconfirmed)}): {', '.join(unconfirmed)}")

    if not any((closed, cancelled, test_orders, unconfirmed)):
        print("  Nenhum. Os pedidos estao abertos, confirmados e nao sao de teste.")
        print("  Nesse caso o motivo e a ORDENACAO: a aba ordena por data do")
        print("  pedido, e o processedAt destes esta no passado (2020-2025),")
        print("  entao eles caem no fim da lista.")

    store = settings.store_domain.replace(".myshopify.com", "")
    print(f"\n  Link direto (busca por tag, ignora ordenacao):")
    print(f"    https://admin.shopify.com/store/{store}/orders?query=tag%3Abagy-import")

    return 0


if __name__ == "__main__":
    sys.exit(main())
