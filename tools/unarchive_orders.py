"""Desarquiva os pedidos importados que a Shopify fechou sozinha.

A loja tem ligado, por padrao, "arquivar automaticamente o pedido depois de pago
e enviado" (Settings -> Checkout -> Order processing). Como a migracao cria o
pedido ja pago e o fulfillment o completa, ele nasce e e arquivado no mesmo dia -
e a aba de Pedidos, no filtro padrao, esconde arquivados.

Arquivar e o comportamento correto para um pedido concluido. Este script existe
so para quando voce quer os pedidos de teste visiveis na aba principal.

Uso:
    python tools/unarchive_orders.py --dry-run
    python tools/unarchive_orders.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.errorlog import ErrorLog
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError, ShopifyUserError

FIND_CLOSED_QUERY = """
query FindClosedBagyOrders($query: String!) {
  orders(first: 50, query: $query) {
    nodes {
      id
      name
      closed
      closedAt
      displayFinancialStatus
      displayFulfillmentStatus
    }
  }
}
"""

ORDER_OPEN_MUTATION = """
mutation OrderOpen($input: OrderOpenInput!) {
  orderOpen(input: $input) {
    order {
      id
      name
      closed
    }
    userErrors {
      field
      message
    }
  }
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Desarquiva os pedidos importados da Bagy que estao fechados."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra o que faria, sem alterar nada")
    parser.add_argument("--tag", default="bagy-import",
                        help="tag usada para achar os pedidos (padrao: bagy-import)")
    args = parser.parse_args()

    settings = get_settings()
    errors_log = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    try:
        data = client.execute(FIND_CLOSED_QUERY, {"query": f"tag:{args.tag} AND status:closed"})
    except ShopifyError as exc:
        print(f"ERRO ao listar: {exc}")
        errors_log.record(script="unarchive", stage="listar", message=str(exc), error=exc)
        return 1

    closed = [n for n in ((data.get("orders") or {}).get("nodes") or []) if n["closed"]]

    if not closed:
        print(f"Nenhum pedido arquivado com a tag {args.tag}.")
        return 0

    print(f"{len(closed)} pedido(s) arquivado(s):\n")
    for node in closed:
        print(f"  {node['name']}  arquivado em {node['closedAt']}  "
              f"({node['displayFinancialStatus']}/{node['displayFulfillmentStatus']})")

    if args.dry_run:
        print("\n(dry-run) nada foi alterado.")
        return 0

    print()
    reopened, failed = [], []

    for node in closed:
        try:
            result = client.execute(ORDER_OPEN_MUTATION, {"input": {"id": node["id"]}})
            payload = result.get("orderOpen") or {}
            user_errors = payload.get("userErrors") or []
            if user_errors:
                raise ShopifyUserError(user_errors, request_id=client.last_request_id)
            order = payload.get("order") or {}
            print(f"[ok] {node['name']}: desarquivado (closed={order.get('closed')})")
            reopened.append(node["name"])
        except (ShopifyUserError, ShopifyError) as exc:
            print(f"[!!] {node['name']}: {exc}")
            errors_log.record(script="unarchive", stage="orderOpen",
                              message=str(exc), error=exc,
                              context={"pedido": node["name"], "id": node["id"]})
            failed.append(node["name"])

    print(f"\nDesarquivados: {len(reopened)}   Falhas: {len(failed)}")
    if errors_log.count:
        print(errors_log.summary())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
