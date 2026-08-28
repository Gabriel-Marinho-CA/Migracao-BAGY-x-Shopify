"""Confere o CPF/CNPJ no registro do cliente (aba Clientes do admin).

Mostra a definicao do metafield (precisa estar fixada) e o valor gravado em
cada cliente que veio da migracao.

Uso: python tools/check_customer_document.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings
from bagy2shopify.shopify_client import (
    CUSTOMER_DOCUMENT_KEY,
    CUSTOMER_DOCUMENT_NAMESPACE,
    ShopifyClient,
    ShopifyError,
)

DEFINITION_QUERY = """
query CustomerDocumentDefinition($namespace: String!, $key: String!) {
  metafieldDefinitions(ownerType: CUSTOMER, namespace: $namespace, key: $key, first: 1) {
    nodes {
      id
      name
      namespace
      key
      type { name }
      pinnedPosition
      description
    }
  }
}
"""

CUSTOMERS_QUERY = """
query ImportedCustomers($namespace: String!, $key: String!) {
  customers(first: 30, query: "tag:bagy-import") {
    nodes {
      id
      displayName
      defaultEmailAddress { emailAddress }
      metafield(namespace: $namespace, key: $key) {
        value
        definition { name pinnedPosition }
      }
    }
  }
}
"""


def main() -> int:
    settings = get_settings()
    client = ShopifyClient(settings.store_domain, settings.admin_token, settings.api_version)
    variables = {"namespace": CUSTOMER_DOCUMENT_NAMESPACE, "key": CUSTOMER_DOCUMENT_KEY}

    try:
        nodes = (client.execute(DEFINITION_QUERY, variables)
                 .get("metafieldDefinitions") or {}).get("nodes") or []
    except ShopifyError as exc:
        print(f"ERRO ao ler a definicao: {exc}")
        return 1

    print("Definicao do metafield")
    if not nodes:
        print(f"  NAO EXISTE ({CUSTOMER_DOCUMENT_NAMESPACE}.{CUSTOMER_DOCUMENT_KEY})")
        print("  Sem ela o admin nao mostra o campo na pagina do cliente.")
        return 1

    definition = nodes[0]
    fixada = definition.get("pinnedPosition")
    print(f"  nome      : {definition['name']}")
    print(f"  chave     : {definition['namespace']}.{definition['key']}")
    print(f"  tipo      : {(definition.get('type') or {}).get('name')}")
    print(f"  fixada    : {'sim (posicao ' + str(fixada) + ')' if fixada is not None else 'NAO - nao aparece no admin'}")

    try:
        customers = (client.execute(CUSTOMERS_QUERY, variables)
                     .get("customers") or {}).get("nodes") or []
    except ShopifyError as exc:
        print(f"\nERRO ao listar clientes: {exc}")
        return 1

    print(f"\nClientes importados: {len(customers)}\n")
    sem_documento = 0
    for customer in customers:
        metafield = customer.get("metafield") or {}
        valor = metafield.get("value")
        if not valor:
            sem_documento += 1
        email = (customer.get("defaultEmailAddress") or {}).get("emailAddress", "-")
        print(f"  {customer['displayName']:<26} {valor or '(sem documento)':<16} {email}")

    print(f"\n  com CPF/CNPJ: {len(customers) - sem_documento}   sem: {sem_documento}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
