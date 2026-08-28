"""Aplica CPF/CNPJ e "Informacoes adicionais" nos pedidos JA migrados.

Os primeiros pedidos foram criados jogando tudo no campo de observacoes. Este
script move aquilo para os lugares certos do admin, sem recriar nada:

* CPF/CNPJ  -> localizedFields TAX_CREDENTIAL_BR
               (card do Cliente, bloco "Informacoes adicionais")
* o resto   -> customAttributes
               (card "Informacoes adicionais", em pares chave/valor)
* note      -> passa a ter so as observacoes em texto livre da Bagy

Atencao: `customAttributes` no orderUpdate SOBRESCREVE a lista inteira. Como a
migracao e a unica fonte desses atributos, isso e o desejado aqui.

Uso:
    python tools/backfill_attributes.py --dry-run
    python tools/backfill_attributes.py
    python tools/backfill_attributes.py --order 1167
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.bagy_client import MockBagyClient, iter_orders
from bagy2shopify.config import get_settings
from bagy2shopify.errorlog import ErrorLog
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError, ShopifyUserError
from bagy2shopify.transform import (
    build_custom_attributes,
    build_localized_fields,
    build_note,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reaplica CPF/CNPJ e informacoes adicionais nos pedidos migrados."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="mostra o que seria gravado, sem alterar")
    parser.add_argument("--order", action="append", metavar="ID",
                        help="so o(s) pedido(s) informado(s)")
    parser.add_argument("--verbose", action="store_true",
                        help="lista todos os atributos")
    args = parser.parse_args()

    settings = get_settings()
    errors_log = ErrorLog(settings.log_dir, include_payload=settings.log_payloads)
    client = ShopifyClient(
        settings.store_domain, settings.admin_token, settings.api_version, logger=print
    )

    if not client.detect_order_read_access():
        print("ERRO: sem acesso de leitura ao objeto Order.")
        print("      Requer aprovacao de Protected Customer Data.")
        return 2

    print(f"Loja : {client.shop_info()['name']}")
    print(f"Modo : {'DRY-RUN' if args.dry_run else 'ENVIO REAL'}")
    print("-" * 72)

    bagy = MockBagyClient(settings.mock_dir)
    source = ((bagy.get_order(str(o)) for o in args.order) if args.order
              else iter_orders(bagy))

    updated, skipped, failed = [], [], []

    for order in source:
        bagy_id = str(order.get("id"))
        label = f"Pedido Bagy #{bagy_id}"

        try:
            shopify_order = client.find_order_by_bagy_id(bagy_id)
        except ShopifyError as exc:
            print(f"[!!] {label}: falha ao localizar - {exc}")
            errors_log.record(script="backfill", stage="localizar", bagy_id=bagy_id,
                              message=str(exc), error=exc)
            failed.append(bagy_id)
            continue

        if not shopify_order:
            print(f"[--] {label}: nao encontrado na loja")
            skipped.append(bagy_id)
            continue

        name = shopify_order["name"]
        attributes = build_custom_attributes(order)
        localized = build_localized_fields(order)
        note = build_note(order)

        document = localized[0]["value"] if localized else "(sem CPF/CNPJ)"
        print(f"\n>>> {label} -> {name}")
        print(f"    CPF/CNPJ   : {document}")
        print(f"    atributos  : {len(attributes)}")
        print(f"    nota       : {note.splitlines()[0][:60] if note else '(vazia)'}")

        if args.verbose:
            for attribute in attributes:
                print(f"      {attribute['key']:<30} {attribute['value'][:44]}")

        if args.dry_run:
            print(f"[ok] {label}: seria atualizado (dry-run)")
            updated.append(bagy_id)
            continue

        order_input: dict = {"id": shopify_order["id"], "customAttributes": attributes}
        if localized:
            order_input["localizedFields"] = localized
        # Sobrescreve a nota antiga, que tinha os dados agora estruturados.
        order_input["note"] = note

        try:
            result = client.update_order(order_input)
        except (ShopifyUserError, ShopifyError) as exc:
            # A Shopify valida CPF/CNPJ. Se recusar, os customAttributes iriam
            # junto na mesma chamada e se perderiam - por isso repete sem o
            # documento em vez de desistir do pedido inteiro.
            if "localizationExtensions" in str(exc) or "Localization extension" in str(exc):
                print(f"     aviso: CPF/CNPJ {document} recusado pela Shopify; "
                      "gravando o resto sem ele")
                errors_log.record(
                    script="backfill", stage="orderUpdate-localizedFields",
                    bagy_id=bagy_id, message=str(exc), error=exc,
                    payload={"localizedFields": localized},
                    context={"pedido_shopify": name, "documento": document},
                )
                order_input.pop("localizedFields", None)
                try:
                    result = client.update_order(order_input)
                except (ShopifyUserError, ShopifyError) as retry_exc:
                    print(f"[!!] {label}: {retry_exc}")
                    errors_log.record(script="backfill", stage="orderUpdate",
                                      bagy_id=bagy_id, message=str(retry_exc),
                                      error=retry_exc, payload=order_input,
                                      context={"pedido_shopify": name})
                    failed.append(bagy_id)
                    continue
            else:
                print(f"[!!] {label}: {exc}")
                errors_log.record(script="backfill", stage="orderUpdate", bagy_id=bagy_id,
                                  message=str(exc), error=exc, payload=order_input,
                                  context={"pedido_shopify": name})
                failed.append(bagy_id)
                continue

        gravados = len(result.get("customAttributes") or [])
        localizados = (result.get("localizedFields") or {}).get("nodes") or []
        cpf_gravado = localizados[0]["value"] if localizados else "-"
        print(f"[ok] {label} -> {name}: {gravados} atributo(s), CPF/CNPJ {cpf_gravado}")
        updated.append(bagy_id)

    print("\n" + "=" * 72)
    print(f"Atualizados : {len(updated)}")
    print(f"Pulados     : {len(skipped)}")
    print(f"Falhas      : {len(failed)}")
    if errors_log.count:
        print(f"\n{errors_log.summary()}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
