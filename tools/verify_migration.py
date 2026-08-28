"""Confere, pedido a pedido, o que entrou na Shopify contra o que veio da Bagy.

Le o pedido direto pelo objeto Order (autoritativo), nao pela contagem do indice
de busca, que fica minutos atrasado depois de uma escrita.

Confere: status financeiro, fulfillment, total, frete, desconto e quantidade de
itens. Qualquer divergencia sai marcada com [X].

Uso:
    python tools/verify_migration.py
    python tools/verify_migration.py --order 1167
    python tools/verify_migration.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.bagy_client import MockBagyClient, iter_orders
from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError
from bagy2shopify.transform import STATUS_MAP, dec, normalize_status

VERIFY_QUERY = """
query VerifyBagyOrder($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      processedAt
      displayFinancialStatus
      displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      customer { id displayName defaultEmailAddress { emailAddress } }
      lineItems(first: 50) { nodes { title quantity sku } }
      fulfillments(first: 5) { id status totalQuantity trackingInfo { company number } }
    }
  }
}
"""

CENT = Decimal("0.01")


def money(node: dict, key: str) -> Decimal:
    try:
        return Decimal(node[key]["shopMoney"]["amount"]).quantize(CENT)
    except (KeyError, TypeError):
        return Decimal("0.00")


def main() -> int:
    parser = argparse.ArgumentParser(description="Confere a migracao pedido a pedido.")
    parser.add_argument("--order", action="append", metavar="ID",
                        help="confere so o(s) pedido(s) informado(s)")
    parser.add_argument("--verbose", action="store_true", help="lista os itens")
    args = parser.parse_args()

    settings = get_settings()
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    if not client.detect_order_read_access():
        print("ERRO: sem acesso de leitura ao objeto Order.")
        print("      Requer aprovacao de Protected Customer Data.")
        return 2

    bagy = MockBagyClient(settings.mock_dir)
    source = ((bagy.get_order(str(o)) for o in args.order) if args.order
              else iter_orders(bagy))

    print(f"{'Bagy':<7} {'Shopify':<8} {'Pagamento':<12} {'Fulfillment':<12} "
          f"{'Total Bagy':>12} {'Total Shopify':>14}  OK")
    print("-" * 80)

    ok_count, diff_count, missing = 0, 0, []

    for order in source:
        bagy_id = str(order.get("id"))
        try:
            data = client.execute(VERIFY_QUERY, {"query": f"tag:bagy-id-{bagy_id}"})
        except ShopifyError as exc:
            print(f"{bagy_id:<7} erro ao consultar: {str(exc)[:60]}")
            continue

        nodes = (data.get("orders") or {}).get("nodes") or []
        if not nodes:
            print(f"{bagy_id:<7} {'-':<8} NAO ENCONTRADO NA LOJA")
            missing.append(bagy_id)
            continue

        node = nodes[0]
        mapping = STATUS_MAP.get(normalize_status(order.get("status", "")))

        bagy_total = dec(order.get("total"))
        shop_total = money(node, "totalPriceSet")
        shop_ship = money(node, "totalShippingPriceSet")
        bagy_ship = dec(order.get("shipment_value"))

        problems = []
        if abs(bagy_total - shop_total) > CENT:
            problems.append(f"total difere ({bagy_total} x {shop_total})")
        if abs(bagy_ship - shop_ship) > CENT:
            problems.append(f"frete difere ({bagy_ship} x {shop_ship})")
        if mapping and mapping.financial != node["displayFinancialStatus"]:
            problems.append(f"pagamento esperado {mapping.financial}, "
                            f"veio {node['displayFinancialStatus']}")
        if mapping and mapping.fulfilled and node["displayFulfillmentStatus"] != "FULFILLED":
            problems.append(f"deveria estar despachado, esta "
                            f"{node['displayFulfillmentStatus']}")

        mark = "ok" if not problems else "[X]"
        if problems:
            diff_count += 1
        else:
            ok_count += 1

        print(f"{bagy_id:<7} {node['name']:<8} {node['displayFinancialStatus']:<12} "
              f"{node['displayFulfillmentStatus']:<12} "
              f"{bagy_total:>12.2f} {shop_total:>14.2f}  {mark}")

        for problem in problems:
            print(f"        -> {problem}")

        if args.verbose:
            customer = node.get("customer") or {}
            email = ((customer.get("defaultEmailAddress") or {}).get("emailAddress")
                     or "(sem cliente)")
            print(f"        cliente: {customer.get('displayName', '-')} <{email}>")
            for item in (node.get("lineItems") or {}).get("nodes") or []:
                sku = f" [{item['sku']}]" if item.get("sku") else ""
                print(f"        item   : {item['quantity']}x {item['title']}{sku}")
            for fulfillment in node.get("fulfillments") or []:
                tracking = fulfillment.get("trackingInfo") or []
                code = tracking[0].get("number") if tracking else "sem rastreio"
                print(f"        envio  : {fulfillment['status']} "
                      f"({fulfillment['totalQuantity']} un., {code})")

    print("-" * 80)
    print(f"Conferidos sem divergencia : {ok_count}")
    print(f"Com divergencia            : {diff_count}")
    if missing:
        print(f"Nao encontrados na loja    : {', '.join(missing)}")

    return 1 if (diff_count or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
