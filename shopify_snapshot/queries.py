"""Consultas de leitura do snapshot da loja (Admin GraphQL 2026-07).

Tres jeitos de ler, conforme o volume:

* bulk   - bulkOperationRunQuery: a Shopify gera um JSONL em segundo plano, sem
           paginacao nem limite de custo. Para o que pode ter milhares.
* paged  - paginacao por cursor.
* single - uma chamada.

Nenhuma consulta altera a loja: bulkOperationRunQuery e uma mutation, mas so
agenda a exportacao. Cada registro e normalizado so com o que o diff usa.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

BULK_RUN = """
mutation SnapshotBulkRun($query: String!) {
  bulkOperationRunQuery(query: $query, groupObjects: false) {
    bulkOperation {
      id
      status
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

BULK_STATUS = """
query SnapshotBulkStatus($id: ID!) {
  bulkOperation(id: $id) {
    id
    status
    errorCode
    objectCount
    rootObjectCount
    url
    query
  }
}
"""


def gid_type(gid) -> str:
    parts = str(gid or "").split("/")
    return parts[3] if len(parts) > 4 and parts[0] == "gid:" else ""


def _value(node) -> str | None:
    return ((node or {}).get("metafield") or {}).get("value")


def _lower(value) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def _children(node: dict, type_name: str) -> list:
    return [child for child in node.get("_children") or [] if gid_type(child.get("id")) == type_name]


def _tokens(*values) -> list:
    """Numeros longos (10+ digitos) soltos no pedido: e onde o codigo da Bagy aparece."""
    found = set()
    for value in values:
        if value:
            found.update(re.findall(r"\d{10,}", str(value)))
    return sorted(found)


def parse_jsonl(lines, normalize: Callable) -> list:
    """JSONL da bulk operation -> registros normalizados.

    Itens de conexoes aninhadas (variantes, contas de credito) vem em linhas
    proprias com `__parentId`; aqui voltam para dentro do pai em `_children`.
    """
    nodes, order, children = {}, [], []
    for raw in lines:
        if not raw or not raw.strip():
            continue
        obj = json.loads(raw)
        parent = obj.pop("__parentId", None)
        if parent:
            children.append((parent, obj))
        else:
            nodes[obj["id"]] = obj
            order.append(obj["id"])
    for parent, child in children:
        if parent in nodes:
            nodes[parent].setdefault("_children", []).append(child)
    return [normalize(nodes[gid]) for gid in order]


# --------------------------------------------------------------------------- #
# Normalizadores

def norm_shop(data: dict) -> tuple:
    shop = data.get("shop") or {}
    record = {
        "id": shop.get("id"),
        "name": shop.get("name"),
        "domain": shop.get("myshopifyDomain"),
        "currency": shop.get("currencyCode"),
        "timezone": shop.get("ianaTimezone"),
        "created_at": shop.get("createdAt"),
    }
    installation = data.get("currentAppInstallation") or {}
    scopes = sorted(scope["handle"] for scope in installation.get("accessScopes") or [])
    return [record], {"access_scopes": scopes, "shop": record}


def norm_policies(data: dict) -> tuple:
    policies = (data.get("shop") or {}).get("shopPolicies") or []
    return [{"id": p.get("id"), "type": p.get("type"), "body": p.get("body")} for p in policies], {}


def norm_location(node: dict) -> dict:
    return {"id": node["id"], "name": node.get("name"), "is_active": node.get("isActive"),
            "is_primary": node.get("isPrimary"), "fulfills_online_orders": node.get("fulfillsOnlineOrders")}


def norm_publication(node: dict) -> dict:
    return {"id": node["id"], "name": node.get("name"),
            "catalog_title": (node.get("catalog") or {}).get("title")}


def norm_metafield_definition(node: dict) -> dict:
    return {"id": node["id"], "owner_type": node.get("ownerType"), "namespace": node.get("namespace"),
            "key": node.get("key"), "name": node.get("name"), "type": (node.get("type") or {}).get("name"),
            "validations": [{"name": v.get("name"), "value": v.get("value")} for v in node.get("validations") or []]}


def norm_collection(node: dict) -> dict:
    return {"id": node["id"], "handle": node.get("handle"), "title": node.get("title"),
            "category_id": _value(node)}


def norm_product(node: dict) -> dict:
    variants = [{"id": child["id"], "sku": child.get("sku"), "barcode": child.get("barcode"),
                 "variation_id": _value(child)} for child in _children(node, "ProductVariant")]
    return {"id": node["id"], "handle": node.get("handle"), "title": node.get("title"),
            "status": node.get("status"), "created_at": node.get("createdAt"),
            "product_id": _value(node), "variants": variants}


def norm_customer(node: dict) -> dict:
    accounts = [{"id": child["id"], "balance": (child.get("balance") or {}).get("amount"),
                 "currency": (child.get("balance") or {}).get("currencyCode")}
                for child in _children(node, "StoreCreditAccount")]
    return {
        "id": node["id"],
        "email": _lower((node.get("defaultEmailAddress") or {}).get("emailAddress")),
        "phone": (node.get("defaultPhoneNumber") or {}).get("phoneNumber"),
        "customer_id": _value(node),
        "tags": node.get("tags") or [],
        "created_at": node.get("createdAt"),
        "orders": node.get("numberOfOrders"),
        "store_credit": accounts,
    }


def norm_order(node: dict) -> dict:
    attributes = node.get("customAttributes") or []
    money = (node.get("totalPriceSet") or {}).get("shopMoney") or {}
    return {
        "id": node["id"],
        "name": node.get("name"),
        "source_identifier": node.get("sourceIdentifier"),
        "source_name": node.get("sourceName"),
        "app": (node.get("app") or {}).get("name"),
        "tags": node.get("tags") or [],
        "processed_at": node.get("processedAt"),
        "created_at": node.get("createdAt"),
        "cancelled_at": node.get("cancelledAt"),
        "test": node.get("test"),
        "total": money.get("amount"),
        "currency": money.get("currencyCode"),
        "attribute_keys": [a.get("key") for a in attributes],
        # A nota e os atributos nao sao guardados: so os numeros longos, para
        # achar o codigo do pedido da Bagy onde a migracao anterior o colocou.
        "tokens": _tokens(node.get("name"), node.get("sourceIdentifier"), node.get("poNumber"),
                          node.get("note"), " ".join(node.get("tags") or []),
                          " ".join(str(a.get("value") or "") for a in attributes)),
    }


def norm_discount(node: dict) -> dict:
    discount = node.get("codeDiscount") or {}
    return {
        "id": node["id"],
        "type": discount.get("__typename"),
        "title": discount.get("title"),
        "status": discount.get("status"),
        "codes": [c.get("code") for c in (discount.get("codes") or {}).get("nodes") or []],
        "codes_count": (discount.get("codesCount") or {}).get("count"),
    }


def norm_metaobject_definition(node: dict) -> dict:
    return {"id": node["id"], "type": node.get("type"), "name": node.get("name"),
            "fields": [{"key": field.get("key"), "type": (field.get("type") or {}).get("name")}
                       for field in node.get("fieldDefinitions") or []]}


def norm_metaobject(node: dict) -> dict:
    return {"id": node["id"], "type": node.get("type"), "handle": node.get("handle")}


def norm_file(node: dict) -> dict:
    url = (node.get("image") or {}).get("url") or node.get("url") or ""
    filename = url.split("?")[0].rsplit("/", 1)[-1].lower() if url else None
    return {"id": node["id"], "filename": filename, "status": node.get("fileStatus")}


def norm_handle(node: dict) -> dict:
    return {"id": node["id"], "handle": node.get("handle"), "title": node.get("title")}


def norm_article(node: dict) -> dict:
    blog = node.get("blog") or {}
    return {"id": node["id"], "handle": node.get("handle"), "title": node.get("title"),
            "blog_id": blog.get("id"), "blog_handle": blog.get("handle")}


def norm_menu(node: dict) -> dict:
    return {"id": node["id"], "handle": node.get("handle"), "title": node.get("title"),
            "is_default": node.get("isDefault")}


def norm_redirect(node: dict) -> dict:
    return {"id": node["id"], "path": node.get("path"), "target": node.get("target")}


# --------------------------------------------------------------------------- #
# Documentos

SHOP = """
query SnapshotShop {
  shop {
    id
    name
    myshopifyDomain
    currencyCode
    ianaTimezone
    createdAt
  }
  currentAppInstallation {
    accessScopes {
      handle
    }
  }
}
"""

POLICIES = """
query SnapshotPolicies {
  shop {
    shopPolicies {
      id
      type
      body
    }
  }
}
"""

LOCATIONS = """
query SnapshotLocations($after: String) {
  locations(first: 50, after: $after, includeInactive: false) {
    nodes {
      id
      name
      isActive
      isPrimary
      fulfillsOnlineOrders
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

PUBLICATIONS = """
query SnapshotPublications($after: String) {
  publications(first: 50, after: $after) {
    nodes {
      id
      name
      catalog {
        title
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

METAFIELD_DEFINITIONS = """
query SnapshotMetafieldDefinitions($ownerType: MetafieldOwnerType!, $after: String) {
  metafieldDefinitions(ownerType: $ownerType, first: 250, after: $after) {
    nodes {
      id
      namespace
      key
      name
      ownerType
      type {
        name
      }
      validations {
        name
        value
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

METAOBJECT_DEFINITIONS = """
query SnapshotMetaobjectDefinitions($after: String) {
  metaobjectDefinitions(first: 50, after: $after) {
    nodes {
      id
      type
      name
      fieldDefinitions {
        key
        type {
          name
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

METAOBJECTS = """
query SnapshotMetaobjects($type: String!, $after: String) {
  metaobjects(type: $type, first: 250, after: $after) {
    nodes {
      id
      type
      handle
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

FILES = """
query SnapshotFiles($after: String) {
  files(first: 250, after: $after) {
    nodes {
      id
      fileStatus
      ... on MediaImage {
        image {
          url
        }
      }
      ... on GenericFile {
        url
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

# Tipos de metaobjeto que a migracao cria (product_content.py).
METAOBJECT_TYPES = ("tabela_nutricional", "especificacoes", "depoimentos")


def metaobject_type_sets(store) -> tuple:
    """So consulta tipos que ja existem na loja: metaobjects(type:) de tipo inexistente da erro."""
    existing = {record.get("type") for record in store.records("metaobject_definitions")}
    return tuple({"type": type_} for type_ in METAOBJECT_TYPES if type_ in existing)


COLLECTIONS = """
query SnapshotCollections($after: String) {
  collections(first: 250, after: $after) {
    nodes {
      id
      handle
      title
      metafield(namespace: "bagy", key: "category_id") {
        value
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

PRODUCTS = """
{
  products {
    edges {
      node {
        id
        handle
        title
        status
        createdAt
        metafield(namespace: "bagy", key: "product_id") {
          value
        }
        variants {
          edges {
            node {
              id
              sku
              barcode
              metafield(namespace: "bagy", key: "variation_id") {
                value
              }
            }
          }
        }
      }
    }
  }
}
"""

CUSTOMERS = """
{
  customers {
    edges {
      node {
        id
        createdAt
        tags
        numberOfOrders
        defaultEmailAddress {
          emailAddress
        }
        defaultPhoneNumber {
          phoneNumber
        }
        metafield(namespace: "bagy", key: "customer_id") {
          value
        }
        storeCreditAccounts {
          edges {
            node {
              id
              balance {
                amount
                currencyCode
              }
            }
          }
        }
      }
    }
  }
}
"""

ORDERS = """
{
  orders {
    edges {
      node {
        id
        name
        createdAt
        processedAt
        cancelledAt
        sourceIdentifier
        sourceName
        poNumber
        note
        tags
        test
        app {
          name
        }
        customAttributes {
          key
          value
        }
        totalPriceSet {
          shopMoney {
            amount
            currencyCode
          }
        }
      }
    }
  }
}
"""

# Custo por pagina ~ 50 x (1 + 10 codigos): abaixo do teto de 1000 pontos.
DISCOUNTS = """
query SnapshotCodeDiscounts($after: String) {
  codeDiscountNodes(first: 50, after: $after) {
    nodes {
      id
      codeDiscount {
        __typename
        ... on DiscountCodeBasic {
          title
          status
          codesCount {
            count
          }
          codes(first: 10) {
            nodes {
              code
            }
          }
        }
        ... on DiscountCodeFreeShipping {
          title
          status
          codesCount {
            count
          }
          codes(first: 10) {
            nodes {
              code
            }
          }
        }
        ... on DiscountCodeBxgy {
          title
          status
          codesCount {
            count
          }
          codes(first: 10) {
            nodes {
              code
            }
          }
        }
        ... on DiscountCodeApp {
          title
          status
          codesCount {
            count
          }
          codes(first: 10) {
            nodes {
              code
            }
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

BLOGS = """
query SnapshotBlogs($after: String) {
  blogs(first: 50, after: $after) {
    nodes {
      id
      handle
      title
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

ARTICLES = """
query SnapshotArticles($after: String) {
  articles(first: 250, after: $after) {
    nodes {
      id
      handle
      title
      blog {
        id
        handle
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

PAGES = """
query SnapshotPages($after: String) {
  pages(first: 250, after: $after) {
    nodes {
      id
      handle
      title
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

MENUS = """
query SnapshotMenus($after: String) {
  menus(first: 50, after: $after) {
    nodes {
      id
      handle
      title
      isDefault
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

URL_REDIRECTS = """
query SnapshotUrlRedirects($after: String) {
  urlRedirects(first: 250, after: $after) {
    nodes {
      id
      path
      target
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


@dataclass(frozen=True)
class Resource:
    name: str
    description: str
    method: str                 # bulk | paged | single
    query: str
    normalize: Callable
    scope: str                  # o que o app precisa para ler
    connection: str | None = None
    variable_sets: tuple = ({},)


# Ordem de leitura. `shop` vem primeiro: traz os escopos do app.
RESOURCES = {r.name: r for r in (
    Resource("shop", "Loja e escopos do app", "single", SHOP, norm_shop, "-"),
    Resource("policies", "Politicas", "single", POLICIES, norm_policies, "read_legal_policies"),
    Resource("locations", "Locais de estoque", "paged", LOCATIONS, norm_location, "read_locations",
             "locations"),
    Resource("publications", "Canais de venda", "paged", PUBLICATIONS, norm_publication,
             "read_publications", "publications"),
    Resource("metafield_definitions", "Definicoes de metafield", "paged", METAFIELD_DEFINITIONS,
             norm_metafield_definition, "read_products + read_customers", "metafieldDefinitions",
             tuple({"ownerType": owner} for owner in ("PRODUCT", "PRODUCTVARIANT", "COLLECTION", "CUSTOMER"))),
    Resource("metaobject_definitions", "Definicoes de metaobjeto", "paged", METAOBJECT_DEFINITIONS,
             norm_metaobject_definition, "read_metaobject_definitions", "metaobjectDefinitions"),
    Resource("metaobjects", "Metaobjetos da migracao", "paged", METAOBJECTS, norm_metaobject,
             "read_metaobjects", "metaobjects", metaobject_type_sets),
    Resource("files", "Arquivos", "paged", FILES, norm_file, "read_files", "files"),
    Resource("collections", "Colecoes", "paged", COLLECTIONS, norm_collection, "read_products",
             "collections"),
    Resource("products", "Produtos e variantes", "bulk", PRODUCTS, norm_product, "read_products"),
    Resource("customers", "Clientes e saldo de credito", "bulk", CUSTOMERS, norm_customer,
             "read_customers + read_store_credit_accounts + Protected Customer Data (nome, e-mail, telefone)"),
    Resource("discounts", "Descontos com codigo", "paged", DISCOUNTS, norm_discount, "read_discounts",
             "codeDiscountNodes"),
    Resource("blogs", "Blogs", "paged", BLOGS, norm_handle, "read_content", "blogs"),
    Resource("articles", "Artigos", "paged", ARTICLES, norm_article, "read_content", "articles"),
    Resource("pages", "Paginas", "paged", PAGES, norm_handle, "read_online_store_pages", "pages"),
    Resource("menus", "Menus", "paged", MENUS, norm_menu, "read_online_store_navigation", "menus"),
    Resource("url_redirects", "Redirecionamentos", "paged", URL_REDIRECTS, norm_redirect,
             "read_online_store_navigation", "urlRedirects"),
    Resource("orders", "Pedidos", "bulk", ORDERS, norm_order,
             "read_orders + read_all_orders + Protected Customer Data"),
)}

GROUPS = {
    "catalogo": ["locations", "publications", "metafield_definitions", "metaobject_definitions", "metaobjects",
                 "files", "collections", "products"],
    "clientes": ["metafield_definitions", "customers"],
    "marketing": ["discounts"],
    "conteudo": ["policies", "blogs", "articles", "pages", "menus", "url_redirects"],
    "pedidos": ["orders"],
}


def select(only) -> list:
    """Recursos pedidos, na ordem de leitura. `shop` sempre entra (escopos)."""
    if not only:
        return list(RESOURCES.values())
    chosen = {"shop"}
    for raw in only:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token in GROUPS:
                chosen.update(GROUPS[token])
            elif token in RESOURCES:
                chosen.add(token)
            else:
                raise ValueError(f"recurso ou grupo desconhecido: {token!r}")
    return [resource for name, resource in RESOURCES.items() if name in chosen]
