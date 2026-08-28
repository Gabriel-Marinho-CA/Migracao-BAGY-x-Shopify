"""Mostra um pedido como o admin exibe: cliente, CPF/CNPJ e informacoes adicionais.

Uso: python tools/show_order_card.py 1167
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import ShopifyClient, ShopifyError

QUERY = """
query OrderCard($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      name
      note
      customer {
        displayName
        defaultEmailAddress { emailAddress }
      }
      localizedFields(first: 5) {
        nodes { key title value }
      }
      customAttributes { key value }
      shippingAddress {
        address1
        address2
        city
        provinceCode
        zip
        phone
      }
    }
  }
}
"""


def main() -> int:
    bagy_id = sys.argv[1] if len(sys.argv) > 1 else "1167"
    settings = get_settings()
    client = ShopifyClient(settings.store_domain, settings.admin_token, settings.api_version)

    try:
        data = client.execute(QUERY, {"query": f"tag:bagy-id-{bagy_id}"})
    except ShopifyError as exc:
        print(f"ERRO: {exc}")
        return 1

    nodes = (data.get("orders") or {}).get("nodes") or []
    if not nodes:
        print(f"Pedido Bagy #{bagy_id} nao encontrado.")
        return 1

    node = nodes[0]
    customer = node.get("customer") or {}
    email = (customer.get("defaultEmailAddress") or {}).get("emailAddress", "-")
    address = node.get("shippingAddress") or {}

    print(f"=== {node['name']}  (Bagy #{bagy_id}) ===\n")
    print("Cliente")
    print(f"  {customer.get('displayName', '-')}")
    print("\nInformacoes de contato")
    print(f"  {email}")

    print("\nInformacoes adicionais (card do Cliente)")
    localized = (node.get("localizedFields") or {}).get("nodes") or []
    if localized:
        for field in localized:
            print(f"  {field.get('title') or field['key']}:")
            print(f"  {field['value']}")
    else:
        print("  (vazio)")

    print("\nEndereco de entrega")
    print(f"  {address.get('address1', '-')}")
    if address.get("address2"):
        print(f"  {address['address2']}")
    print(f"  {address.get('zip', '')} {address.get('city', '')} "
          f"{address.get('provinceCode', '')}")
    if address.get("phone"):
        print(f"  {address['phone']}")

    print("\nInformacoes adicionais (card do Pedido)")
    for attribute in node.get("customAttributes") or []:
        print(f"  {attribute['key']}")
        print(f"  {attribute['value']}")

    print(f"\nObservacoes")
    print(f"  {node.get('note') or '(vazia)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
