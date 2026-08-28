"""Despacha, nos pedidos JA migrados, os que estavam finalizados na Bagy.

Contexto: os pedidos foram criados quando o app nao tinha `read_locations`,
entao entraram sem fulfillment. Corrigir na criacao nao da - pedido na Shopify
nao se apaga, e recriar geraria duplicata. Este script corrige no lugar, via
`fulfillmentCreate` sobre os fulfillment orders do pedido existente.

PRE-REQUISITO: aprovacao de Protected Customer Data no Partner Dashboard.
Sem ela nao ha como sequer localizar o pedido pela API.

Uso:
    python fix_fulfillment.py --dry-run
    python fix_fulfillment.py
"""

from __future__ import annotations

import argparse
import sys

from .bagy_client import MockBagyClient, iter_orders
from .config import get_settings
from .errorlog import ErrorLog
from .transform import STATUS_MAP, normalize_status

try:
    from .shopify_client import ShopifyClient, ShopifyError, ShopifyUserError
except ImportError:
    ShopifyClient = None

    class ShopifyError(RuntimeError):
        pass

    class ShopifyUserError(RuntimeError):
        pass


OK = "[ok]"
SKIP = "[--]"
FAIL = "[!!]"


def tracking_info(order: dict) -> dict | None:
    """Monta o trackingInfo a partir dos dados da Bagy."""
    number = (order.get("sending_code") or "").strip()
    if not number:
        return None
    info = {"number": number}
    company = (order.get("shipment_integrator") or "").strip()
    if company:
        info["company"] = company
    url = (order.get("tracking_url") or "").strip()
    if url:
        info["url"] = url
    return info


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fix_fulfillment",
        description="Despacha os pedidos ja migrados que estavam finalizados na Bagy.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra o que faria, sem enviar nada")
    parser.add_argument("--order-id", action="append", default=None, metavar="ID",
                        help="corrige apenas o(s) pedido(s) informado(s)")
    args = parser.parse_args(argv)

    settings = get_settings()
    errors_log = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)

    if not settings.has_credentials:
        print("ERRO: credenciais ausentes. Preencha o .env.")
        return 2
    if ShopifyClient is None:
        print("ERRO: `requests` nao instalado. Rode: pip install -r requirements.txt")
        return 2

    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version,
        orders_per_minute=settings.orders_per_minute,
        max_retries=settings.max_retries, logger=print,
    )

    shop = client.shop_info()
    print(f"Loja : {shop['name']} ({shop['myshopifyDomain']})")
    print(f"API  : {settings.api_version}")

    if not client.detect_order_read_access():
        print("\nERRO: o app ainda nao tem aprovacao de Protected Customer Data.")
        print("      Sem ela nao da para localizar os pedidos pela API.")
        print("      Libere em: Partner Dashboard -> seu app -> API access")
        print("      -> Protected customer data access. Depois rode este script.")
        return 2

    print(f"Modo : {'DRY-RUN' if args.dry_run else 'ENVIO REAL'}")
    print("-" * 72)

    bagy = MockBagyClient(settings.mock_dir)
    if args.order_id:
        source = (bagy.get_order(str(oid)) for oid in args.order_id)
    else:
        source = iter_orders(bagy)

    fixed, skipped, failed = [], [], []

    for order in source:
        bagy_id = str(order.get("id"))
        mapping = STATUS_MAP.get(normalize_status(order.get("status", "")))

        if mapping is None or not mapping.fulfilled:
            continue

        label = f"Pedido Bagy #{bagy_id} ({order.get('status')})"

        try:
            shopify_order = client.find_order_by_bagy_id(bagy_id)
        except ShopifyError as exc:
            print(f"{FAIL} {label}: falha ao localizar - {exc}")
            errors_log.record(script="fix_fulfillment", stage="localizar",
                              bagy_id=bagy_id, message=str(exc), error=exc)
            failed.append((bagy_id, str(exc)))
            continue

        if not shopify_order:
            print(f"{SKIP} {label}: nao encontrado na loja (migre primeiro)")
            skipped.append(bagy_id)
            continue

        name = shopify_order["name"]
        status = shopify_order["displayFulfillmentStatus"]

        if status == "FULFILLED":
            print(f"{SKIP} {label} -> {name}: ja despachado")
            skipped.append(bagy_id)
            continue

        pending = [
            fo for fo in (shopify_order.get("fulfillmentOrders") or {}).get("nodes") or []
            if fo.get("status") in ("OPEN", "IN_PROGRESS")
            and any(li.get("remainingQuantity", 0) > 0
                    for li in (fo.get("lineItems") or {}).get("nodes") or [])
        ]

        if not pending:
            print(f"{SKIP} {label} -> {name}: nenhum fulfillment order aberto")
            skipped.append(bagy_id)
            continue

        fulfillment_input: dict = {
            "notifyCustomer": False,
            "lineItemsByFulfillmentOrder": [
                {"fulfillmentOrderId": fo["id"]} for fo in pending
            ],
        }
        tracking = tracking_info(order)
        if tracking:
            fulfillment_input["trackingInfo"] = tracking

        qty = sum(
            li["remainingQuantity"]
            for fo in pending
            for li in (fo.get("lineItems") or {}).get("nodes") or []
        )
        rastreio = tracking.get("number") if tracking else "sem rastreio"
        print(f"\n>>> {label} -> {name}")
        print(f"    situacao   : {status}")
        print(f"    a despachar: {qty} unidade(s) em {len(pending)} fulfillment order(s)")
        print(f"    rastreio   : {rastreio}")

        if args.dry_run:
            print(f"{OK} {label}: seria despachado (dry-run)")
            fixed.append(bagy_id)
            continue

        try:
            fulfillment = client.create_fulfillment(fulfillment_input)
        except (ShopifyUserError, ShopifyError) as exc:
            kind = "userErrors" if isinstance(exc, ShopifyUserError) else "erro"
            print(f"{FAIL} {label}: {kind} -> {exc}")
            entry = errors_log.record(
                script="fix_fulfillment", stage="fulfillmentCreate", bagy_id=bagy_id,
                message=str(exc), error=exc, payload={"fulfillment": fulfillment_input},
                context={"pedido_shopify": name, "status": status},
            )
            if entry.get("request_id"):
                print(f"    X-Request-Id: {entry['request_id']}")
            failed.append((bagy_id, f"{kind}: {exc}"))
            continue

        print(f"{OK} {label} -> {name}: despachado "
              f"({fulfillment['status']}, {fulfillment['totalQuantity']} unidade(s))")
        fixed.append(bagy_id)

    print("\n" + "=" * 72)
    print(f"Despachados : {len(fixed)}")
    print(f"Pulados     : {len(skipped)}")
    print(f"Falhas      : {len(failed)}")
    if failed:
        print("\nFalhas:")
        for bagy_id, reason in failed:
            print(f"  #{bagy_id}: {reason}")

    if errors_log.count:
        print(f"\n{errors_log.summary()}")
        print("Para revisar: python tools/show_errors.py")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
