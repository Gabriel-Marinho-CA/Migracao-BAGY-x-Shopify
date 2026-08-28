"""CLI da migracao Bagy -> Shopify.

Exemplos:
    python -m bagy2shopify.migrate --dry-run
    python -m bagy2shopify.migrate --dry-run --order-id 1167
    python -m bagy2shopify.migrate --check              # so testa as credenciais
    python -m bagy2shopify.migrate --limit 2            # cria de verdade
    python -m bagy2shopify.migrate                      # migra tudo

Idempotencia: cada pedido criado fica registrado em state/migrated.json com o
GID da Shopify. Rodar de novo pula o que ja foi. Use --force para reenviar.
(O ledger local e usado em vez de consultar a Shopify porque, sem o escopo
`read_all_orders`, pedidos com mais de 60 dias nao sao legiveis pela API.)
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .bagy_client import MockBagyClient, iter_orders
from .config import get_settings
from .errorlog import ErrorLog
from .fix_fulfillment import sweep_fulfillment
from .transform import to_shopify_order

try:
    from .shopify_client import ShopifyClient, ShopifyError, ShopifyUserError
except ImportError:  # `requests` ausente: o fluxo --dry-run continua valendo
    ShopifyClient = None

    class ShopifyError(RuntimeError):
        pass

    class ShopifyUserError(RuntimeError):
        pass


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[aviso] {path} corrompido - comecando um ledger novo")
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Saida
# --------------------------------------------------------------------------- #

OK = "[ok]"
SKIP = "[--]"
FAIL = "[!!]"
WARN = "     aviso:"


def print_summary(result, order: dict) -> None:
    oi = result.order_input
    items = oi.get("lineItems", [])
    total = "?"
    for transaction in oi.get("transactions", []):
        total = transaction["amountSet"]["shopMoney"]["amount"]
    discount = oi.get("discountCode", {}).get("itemFixedDiscountCode", {})

    print(f"       status Bagy : {order.get('status')} -> {oi.get('financialStatus')}"
          f"{' + fulfillment' if 'fulfillment' in oi else ''}")
    print(f"       cliente     : {oi.get('email') or '(sem e-mail)'}")
    print(f"       itens       : {len(items)} linha(s), "
          f"{sum(i['quantity'] for i in items)} unidade(s)")
    if oi.get("shippingLines"):
        ship = oi["shippingLines"][0]
        print(f"       frete       : {ship['title']} "
              f"R$ {ship['priceSet']['shopMoney']['amount']}")
    if discount:
        print(f"       desconto    : {discount.get('code')} "
              f"R$ {discount['amountSet']['shopMoney']['amount']}")
    print(f"       total pago  : R$ {total}" if total != "?" else "       total pago  : (nao pago)")


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #

def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bagy2shopify",
        description="Migra pedidos da Bagy para a Shopify via Admin GraphQL API.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="transforma e grava os payloads, sem chamar a Shopify")
    parser.add_argument("--check", action="store_true",
                        help="valida credenciais e mostra dados da loja, sem migrar")
    parser.add_argument("--order-id", action="append", default=None, metavar="ID",
                        help="migra apenas o(s) pedido(s) informado(s); pode repetir")
    parser.add_argument("--limit", type=int, default=None,
                        help="processa no maximo N pedidos")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="le no maximo N paginas da listagem")
    parser.add_argument("--force", action="store_true",
                        help="reenvia pedidos que ja constam no ledger")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="aborta no primeiro erro (padrao: segue e reporta no fim)")
    parser.add_argument("--no-save-payloads", action="store_true",
                        help="nao grava os JSONs enviados em out/payloads")
    args = parser.parse_args(argv)

    settings = get_settings()
    errors_log = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)

    # ------------------------------------------------------------------ #
    # Conexao (nao necessaria em dry-run)
    # ------------------------------------------------------------------ #
    client = None
    location_id = None

    if not args.dry_run:
        if not settings.has_credentials:
            print("ERRO: credenciais da Shopify ausentes.\n")
            print("  Defina no .env (copie de .env.example):")
            print("    SHOPIFY_STORE_DOMAIN=turbo-starter.myshopify.com")
            print("    SHOPIFY_ADMIN_TOKEN=shpat_...\n")
            print("  O token sai em: Shopify admin -> Settings -> Apps and sales channels")
            print("  -> Develop apps -> <seu app> -> API credentials -> Install app.")
            print("  Atencao: a 'chave secreta' shpss_... NAO serve aqui.\n")
            print("  Enquanto isso, rode com --dry-run para conferir os payloads.")
            return 2

        if ShopifyClient is None:
            print("ERRO: o pacote `requests` nao esta instalado.")
            print("  Rode: pip install -r requirements.txt")
            return 2

        client = ShopifyClient(
            settings.store_domain,
            settings.admin_token,
            settings.api_version,
            orders_per_minute=settings.orders_per_minute,
            max_retries=settings.max_retries,
            logger=print,
        )
        try:
            shop = client.shop_info()
        except ShopifyError as exc:
            print(f"ERRO ao conectar na Shopify: {exc}")
            errors_log.record(script="migrate", stage="conexao",
                              message=str(exc), error=exc)
            print(errors_log.summary())
            return 2

        print(f"Loja      : {shop['name']} ({shop['myshopifyDomain']})")
        print(f"Moeda     : {shop['currencyCode']}   Fuso: {shop['ianaTimezone']}")
        print(f"API       : {settings.api_version}")

        if shop["currencyCode"] != settings.currency:
            print(f"\n[aviso] a loja esta em {shop['currencyCode']} mas os pedidos da Bagy "
                  f"estao em {settings.currency}.")
            print("        Os valores serao gravados na moeda da loja, sem conversao.")

        # Sem essa definicao o CPF/CNPJ ate e gravado no cliente, mas o admin
        # nao mostra em "Informacoes adicionais" na pagina dele.
        try:
            print(f"Metafield : definicao CPF/CNPJ do cliente "
                  f"{client.ensure_customer_document_definition()}")
        except (ShopifyUserError, ShopifyError) as exc:
            print(f"[aviso] nao foi possivel garantir a definicao do metafield: {exc}")
            print("        O CPF/CNPJ do cliente pode nao aparecer no admin.")

        location_id = client.primary_location_id()
        print(f"Local     : {location_id or '(nenhum) - pedidos finalizados ficarao sem fulfillment'}")

        if not client.detect_order_read_access():
            print("\n[aviso] o app nao tem aprovacao de Protected Customer Data.")
            print("        Criar pedidos funciona; LER o objeto Order e negado, inclusive")
            print("        o `order` devolvido pelo proprio orderCreate.")
            print("        -> usando a mutation minima: o pedido e criado, mas a resposta")
            print("           nao traz GID nem numero. A confirmacao vem por ordersCount.")
            print("        Para liberar: Partner Dashboard -> seu app -> API access")
            print("        -> Protected customer data access.")

        if args.check:
            total_orders = client.order_count()
            imported = client.order_count("tag:bagy-import")
            print(f"\nPedidos na loja: {total_orders} (importados da Bagy: {imported})")
            print("Credenciais OK.")
            return 0
    elif args.check:
        print("--check exige credenciais; rode sem --dry-run.")
        return 2

    # ------------------------------------------------------------------ #
    # Origem dos pedidos
    # ------------------------------------------------------------------ #
    bagy = MockBagyClient(settings.mock_dir)
    print(f"Origem    : {settings.mock_dir} (mocks)")
    print(f"Modo      : {'DRY-RUN (nada e enviado)' if args.dry_run else 'ENVIO REAL'}")
    print("-" * 72)

    state = load_state(settings.state_file)

    if args.order_id:
        source = (bagy.get_order(str(oid)) for oid in args.order_id)
    else:
        source = iter_orders(bagy, max_pages=args.max_pages)

    payload_dir = settings.payload_dir
    if args.dry_run and not args.no_save_payloads:
        payload_dir.mkdir(parents=True, exist_ok=True)

    created, skipped, failed, processed = [], [], [], 0
    all_warnings = []

    for order in source:
        if args.limit is not None and processed >= args.limit:
            break
        processed += 1

        bagy_id = str(order.get("id"))
        label = f"Pedido Bagy #{bagy_id}"

        if not args.force and bagy_id in state:
            ref = state[bagy_id].get("shopify_gid") or state[bagy_id].get("shopify_name") or "sem GID"
            print(f"{SKIP} {label}: ja migrado ({ref})")
            skipped.append(bagy_id)
            continue

        # Rede de seguranca contra duplicata: o ledger pode nao ter registrado um
        # pedido que a Shopify criou (ex.: leitura da resposta negada). A contagem
        # por tag e dado agregado, entao funciona mesmo sem acesso a Order.
        if not args.force and client is not None:
            try:
                if client.order_exists(bagy_id):
                    print(f"{SKIP} {label}: ja existe na loja (tag bagy-id-{bagy_id})")
                    state.setdefault(bagy_id, {
                        "shopify_gid": None,
                        "migrated_at": datetime.now(timezone.utc).isoformat(),
                        "verified_by": "tag",
                    })
                    save_state(settings.state_file, state)
                    skipped.append(bagy_id)
                    continue
            except ShopifyError as exc:
                print(f"{WARN} nao foi possivel checar duplicata de #{bagy_id}: {exc}")
                errors_log.record(script="migrate", stage="checagem-duplicata",
                                  bagy_id=bagy_id, message=str(exc), error=exc)

        try:
            result = to_shopify_order(
                order,
                currency=settings.currency,
                timezone_offset=settings.timezone_offset,
                location_id=location_id,
                source_name=None,
            )
        except Exception as exc:  # transformacao nunca deve derrubar o lote
            print(f"{FAIL} {label}: erro ao transformar - {exc}")
            traceback.print_exc()
            errors_log.record(script="migrate", stage="transform", bagy_id=bagy_id,
                              message=str(exc), error=exc,
                              context={"status_bagy": order.get("status")})
            failed.append((bagy_id, str(exc)))
            if args.stop_on_error:
                break
            continue

        print(f"\n>>> {label}")
        print_summary(result, order)

        for warning in result.warnings:
            print(f"{WARN} {warning}")
            all_warnings.append((bagy_id, warning))

        if not result.order_input.get("lineItems"):
            print(f"{FAIL} {label}: sem itens - pulado")
            errors_log.record(script="migrate", stage="validacao", bagy_id=bagy_id,
                              message="pedido sem itens utilizaveis",
                              payload=result.order_input,
                              context={"avisos": result.warnings})
            failed.append((bagy_id, "pedido sem itens"))
            continue

        if not args.no_save_payloads:
            payload_dir.mkdir(parents=True, exist_ok=True)
            out = payload_dir / f"order_{bagy_id}.json"
            out.write_text(
                json.dumps(
                    {"order": result.order_input, "options": result.options},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            print(f"       payload     : {out}")

        if args.dry_run:
            print(f"{OK} {label}: payload gerado (dry-run)")
            created.append((bagy_id, "dry-run"))
            continue

        try:
            shopify_order = client.create_order(result.order_input, result.options)
        except (ShopifyUserError, ShopifyError) as exc:
            kind = "userErrors" if isinstance(exc, ShopifyUserError) else "erro"
            print(f"{FAIL} {label}: {kind} -> {exc}")
            entry = errors_log.record(
                script="migrate", stage="orderCreate", bagy_id=bagy_id,
                message=str(exc), error=exc,
                payload={"order": result.order_input, "options": result.options},
                context={"status_bagy": order.get("status"),
                         "total_bagy": order.get("total")},
            )
            if entry.get("request_id"):
                print(f"       X-Request-Id: {entry['request_id']}")
            failed.append((bagy_id, f"{kind}: {exc}"))
            if args.stop_on_error:
                break
            continue

        # O CPF/CNPJ so entra por orderUpdate: OrderCreateOrderInput nao tem
        # localizedFields. Precisa do GID, que exige leitura de Order.
        if result.localized_fields and shopify_order.get("id"):
            try:
                client.update_order({
                    "id": shopify_order["id"],
                    "localizedFields": result.localized_fields,
                })
                print(f"       CPF/CNPJ    : gravado no pedido "
                      f"({result.localized_fields[0]['value']})")

                # O mesmo documento no registro do cliente (aba Clientes), que
                # nao tem localizedFields - la e metafield.
                customer_gid = (shopify_order.get("customer") or {}).get("id")
                if customer_gid:
                    client.set_customer_document(
                        customer_gid, result.localized_fields[0]["value"]
                    )
                    print(f"       CPF/CNPJ    : gravado no cliente")
            except (ShopifyUserError, ShopifyError) as exc:
                print(f"{WARN} nao foi possivel gravar o CPF/CNPJ de #{bagy_id}: {exc}")
                errors_log.record(
                    script="migrate", stage="orderUpdate-localizedFields",
                    bagy_id=bagy_id, message=str(exc), error=exc,
                    payload={"localizedFields": result.localized_fields},
                )
        elif result.localized_fields:
            print(f"{WARN} CPF/CNPJ de #{bagy_id} nao gravado: resposta sem GID "
                  f"(falta Protected Customer Data)")

        # O campo `fulfillment` do orderCreate fecha apenas um fulfillment
        # order. Pedidos com linha de acrescimo ganham outro so para ela e
        # ficariam PARTIALLY_FULFILLED - esta varredura fecha o que sobrou.
        if result.should_fulfill and shopify_order.get("id"):
            try:
                # Pelo GID: a busca por tag ainda nao indexou o pedido recem-criado.
                found = client.get_order_by_gid(shopify_order["id"])
                if found:
                    fulfillment, quantity = sweep_fulfillment(client, order, found)
                    if fulfillment:
                        print(f"       fulfillment : +{quantity} unidade(s) "
                              f"({fulfillment['status']})")
            except (ShopifyUserError, ShopifyError) as exc:
                print(f"{WARN} varredura de fulfillment de #{bagy_id} falhou: {exc}")
                errors_log.record(script="migrate", stage="sweep-fulfillment",
                                  bagy_id=bagy_id, message=str(exc), error=exc)

        entry = {
            "shopify_gid": shopify_order.get("id"),
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        }

        if shopify_order.get("readable"):
            print(f"{OK} {label} -> {shopify_order['name']} ({shopify_order['id']})  "
                  f"{shopify_order['displayFinancialStatus']}/"
                  f"{shopify_order['displayFulfillmentStatus']}")
            entry["shopify_name"] = shopify_order["name"]
            entry["total"] = shopify_order["totalPriceSet"]["shopMoney"]["amount"]
        else:
            # Sem acesso de leitura a Order a resposta nao traz GID. A conferencia
            # fica para o fim: o indice de busca da Shopify e eventualmente
            # consistente, entao checar agora daria falso negativo.
            entry["verified_by"] = "pendente"
            print(f"{OK} {label}: criado (resposta sem GID - falta Protected Customer Data)")

        created.append((bagy_id, entry["shopify_gid"] or "sem GID"))
        state[bagy_id] = entry
        save_state(settings.state_file, state)

    # ------------------------------------------------------------------ #
    # Conferencia final (so faz sentido quando a resposta veio sem GID)
    # ------------------------------------------------------------------ #
    # "nenhuma" vem de uma tentativa de conferencia que falhou: reconfere.
    pending = [bid for bid, e in state.items()
               if e.get("verified_by") in ("pendente", "nenhuma")]
    if client is not None and pending:
        print("\n" + "-" * 72)
        print(f"Conferindo {len(pending)} pedido(s) pela tag...")
        unconfirmed = []
        for bagy_id in pending:
            try:
                if client.order_exists(bagy_id):
                    state[bagy_id]["verified_by"] = "tag"
                else:
                    unconfirmed.append(bagy_id)
            except ShopifyError as exc:
                print(f"{WARN} falha ao conferir #{bagy_id}: {exc}")
                errors_log.record(script="migrate", stage="conferencia",
                                  bagy_id=bagy_id, message=str(exc), error=exc)
        save_state(settings.state_file, state)

        if unconfirmed:
            print(f"{WARN} sem confirmacao: {', '.join(unconfirmed)}")
            print("       O indice de busca da Shopify leva alguns segundos para")
            print("       atualizar. Rode de novo para reconferir - pedidos ja")
            print("       criados serao pulados, nao duplicados.")
        else:
            print(f"{OK} todos confirmados na loja")

        try:
            print(f"     total com a tag bagy-import: {client.order_count('tag:bagy-import')}")
        except ShopifyError:
            pass

    # ------------------------------------------------------------------ #
    # Relatorio
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 72)
    print(f"Processados : {processed}")
    print(f"Criados     : {len(created)}")
    print(f"Pulados     : {len(skipped)}")
    print(f"Falhas      : {len(failed)}")
    print(f"Avisos      : {len(all_warnings)}")

    if failed:
        print("\nFalhas:")
        for bagy_id, reason in failed:
            print(f"  #{bagy_id}: {reason}")

    if errors_log.count:
        print(f"\n{errors_log.summary()}")
        print("Para revisar: python tools/show_errors.py")

    if not args.dry_run and created:
        print(f"\nLedger: {settings.state_file}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
