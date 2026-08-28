"""Cliente da Admin GraphQL API da Shopify.

Cuida do que a migracao precisa alem do GraphQL em si:

* throttle proprio para o teto de 5 `orderCreate`/minuto de lojas dev/trial;
* retry com backoff exponencial em THROTTLED, 429 e 5xx;
* leitura do `Retry-After` e do `throttleStatus` devolvido pela Shopify.
"""

from __future__ import annotations

import json
import random
import time
from collections import deque

import requests


class ShopifyError(RuntimeError):
    """Erro de transporte ou GraphQL (nivel `errors`).

    Carrega o `X-Request-Id` da resposta: e o identificador que o suporte da
    Shopify pede para rastrear uma chamada.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        graphql_errors: list | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.graphql_errors = graphql_errors or []


class ShopifyUserError(RuntimeError):
    """`userErrors` devolvido pela mutation - problema no payload enviado."""

    def __init__(self, errors: list, *, request_id: str | None = None):
        self.errors = errors
        self.request_id = request_id
        self.status = 200
        detail = "; ".join(
            f"{'.'.join(str(p) for p in (e.get('field') or []))}: {e.get('message')}".lstrip(": ")
            for e in errors
        )
        super().__init__(detail or "userErrors sem detalhe")


ORDER_CREATE_MUTATION = """
mutation OrderCreate($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    userErrors {
      field
      message
    }
    order {
      id
      name
      createdAt
      processedAt
      displayFinancialStatus
      displayFulfillmentStatus
      totalPriceSet {
        shopMoney {
          amount
          currencyCode
        }
      }
      customer {
        id
        defaultEmailAddress {
          emailAddress
        }
      }
    }
  }
}
"""

# Usada quando o app nao tem aprovacao de Protected Customer Data: a mutation
# roda e cria o pedido, mas qualquer selecao dentro de `order` e negada.
ORDER_CREATE_MINIMAL_MUTATION = """
mutation OrderCreateMinimal($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
  orderCreate(order: $order, options: $options) {
    userErrors {
      field
      message
    }
  }
}
"""

# `ordersCount` e dado agregado, entao passa mesmo sem Protected Customer Data.
ORDERS_COUNT_QUERY = """
query OrdersCount($query: String) {
  ordersCount(query: $query) {
    count
  }
}
"""

ORDER_READ_PROBE_QUERY = """
query OrderReadProbe {
  orders(first: 1) {
    nodes {
      id
    }
  }
}
"""

# Usadas por fix_fulfillment.py. Exigem acesso ao objeto Order, entao so
# funcionam depois da aprovacao de Protected Customer Data.
FIND_ORDER_BY_TAG_QUERY = """
query FindBagyOrder($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      displayFulfillmentStatus
      displayFinancialStatus
      customer {
        id
      }
      fulfillmentOrders(first: 10) {
        nodes {
          id
          status
          lineItems(first: 100) {
            nodes {
              id
              remainingQuantity
            }
          }
        }
      }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation FulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment {
      id
      status
      totalQuantity
      trackingInfo {
        company
        number
        url
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

# Pelo GID, nao pela busca: `orders(query:)` passa pelo indice de busca, que fica
# minutos atrasado apos uma escrita e devolveria vazio logo depois de criar.
ORDER_BY_GID_QUERY = """
query OrderFulfillmentOrders($id: ID!) {
  order(id: $id) {
    id
    name
    displayFulfillmentStatus
    fulfillmentOrders(first: 10) {
      nodes {
        id
        status
        lineItems(first: 100) {
          nodes {
            id
            remainingQuantity
          }
        }
      }
    }
  }
}
"""

# CPF/CNPJ no registro do CLIENTE. Customer nao tem localizedFields (so Order
# tem), entao vai como metafield - e precisa de definicao fixada para o admin
# exibir em "Informacoes adicionais" na pagina do cliente.
CUSTOMER_DOCUMENT_NAMESPACE = "custom"
CUSTOMER_DOCUMENT_KEY = "cpf_cnpj"

METAFIELD_DEFINITION_CREATE_MUTATION = """
mutation CustomerDocumentDefinition($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) {
    createdDefinition {
      id
      name
      namespace
      key
      pinnedPosition
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

METAFIELDS_SET_MUTATION = """
mutation SetCustomerDocument($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields {
      id
      namespace
      key
      value
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

ORDER_UPDATE_MUTATION = """
mutation OrderUpdate($input: OrderInput!) {
  orderUpdate(input: $input) {
    order {
      id
      name
      customAttributes {
        key
        value
      }
      localizedFields(first: 5) {
        nodes {
          key
          title
          value
        }
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

PRIMARY_LOCATION_QUERY = """
query PrimaryLocation {
  locations(first: 10, includeInactive: false) {
    nodes {
      id
      name
      isActive
      fulfillsOnlineOrders
    }
  }
}
"""

SHOP_INFO_QUERY = """
query ShopInfo {
  shop {
    id
    name
    myshopifyDomain
    currencyCode
    ianaTimezone
  }
}
"""


class RateLimiter:
    """Janela deslizante simples: no maximo N chamadas por minuto."""

    def __init__(self, per_minute: float):
        self.per_minute = max(per_minute, 0.0)
        self._calls: deque = deque()

    def wait(self) -> float:
        if self.per_minute <= 0:
            return 0.0
        window = 60.0
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= window:
            self._calls.popleft()

        slept = 0.0
        if len(self._calls) >= self.per_minute:
            sleep_for = window - (now - self._calls[0]) + 0.25
            if sleep_for > 0:
                time.sleep(sleep_for)
                slept = sleep_for
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= window:
                self._calls.popleft()

        self._calls.append(time.monotonic())
        return slept


class ShopifyClient:
    def __init__(
        self,
        store_domain: str,
        admin_token: str,
        api_version: str,
        *,
        orders_per_minute: float = 5,
        max_retries: int = 5,
        timeout: int = 60,
        logger=None,
    ):
        if not store_domain or not admin_token:
            raise ShopifyError(
                "Faltam credenciais da Shopify. Defina SHOPIFY_STORE_DOMAIN e "
                "SHOPIFY_ADMIN_TOKEN (token shpat_... de custom app)."
            )
        self.url = f"https://{store_domain}/admin/api/{api_version}/graphql.json"
        self.max_retries = max_retries
        self.timeout = timeout
        self.limiter = RateLimiter(orders_per_minute)
        self.log = logger or (lambda msg: None)
        # Definido por detect_order_read_access(); ate la assume o caminho completo.
        self.can_read_orders = True
        # X-Request-Id da ultima resposta: e o que o suporte da Shopify pede.
        self.last_request_id: str | None = None
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": admin_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "bagy2shopify/0.1",
        })

    # ----------------------------------------------------------------- #

    def execute(self, query: str, variables: dict | None = None, *, throttled: bool = False) -> dict:
        payload = {"query": query, "variables": variables or {}}
        attempt = 0

        while True:
            attempt += 1
            if throttled:
                waited = self.limiter.wait()
                if waited:
                    self.log(f"    throttle local: aguardando {waited:.1f}s")

            try:
                response = self.session.post(self.url, data=json.dumps(payload), timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise ShopifyError(
                        f"falha de rede apos {self.max_retries} tentativas: {exc}"
                    ) from exc
                self._backoff(attempt, f"erro de rede ({exc.__class__.__name__})")
                continue

            request_id = response.headers.get("X-Request-Id")
            self.last_request_id = request_id

            if response.status_code == 429 or response.status_code >= 500:
                if attempt > self.max_retries:
                    raise ShopifyError(
                        f"HTTP {response.status_code} apos {self.max_retries} tentativas: "
                        f"{response.text[:400]}",
                        status=response.status_code, request_id=request_id,
                    )
                retry_after = response.headers.get("Retry-After")
                self._backoff(attempt, f"HTTP {response.status_code}", retry_after)
                continue

            if response.status_code == 401:
                raise ShopifyError(
                    "HTTP 401: token invalido ou sem permissao. Confirme que e um "
                    "Admin API access token e nao a chave secreta (shpss_) do app.",
                    status=401, request_id=request_id,
                )
            if response.status_code != 200:
                raise ShopifyError(
                    f"HTTP {response.status_code}: {response.text[:400]}",
                    status=response.status_code, request_id=request_id,
                )

            body = response.json()
            errors = body.get("errors")
            if errors:
                if self._is_throttled(errors):
                    if attempt > self.max_retries:
                        raise ShopifyError(
                            f"THROTTLED apos {self.max_retries} tentativas",
                            status=200, request_id=request_id, graphql_errors=errors,
                        )
                    self._backoff(attempt, "THROTTLED pela Shopify")
                    continue
                raise ShopifyError(
                    json.dumps(errors, ensure_ascii=False)[:600],
                    status=200, request_id=request_id, graphql_errors=errors,
                )

            return body.get("data") or {}

    @staticmethod
    def _is_throttled(errors: list) -> bool:
        return any(
            (e.get("extensions") or {}).get("code") == "THROTTLED"
            or "throttled" in (e.get("message") or "").lower()
            for e in errors
        )

    def _backoff(self, attempt: int, reason: str, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 2.0 ** attempt
        else:
            delay = 2.0 ** attempt
        delay = min(delay, 60.0) + random.uniform(0, 0.5)
        self.log(f"    {reason}: nova tentativa em {delay:.1f}s (tentativa {attempt})")
        time.sleep(delay)

    # ----------------------------------------------------------------- #

    def shop_info(self) -> dict:
        return self.execute(SHOP_INFO_QUERY)["shop"]

    def primary_location_id(self) -> str | None:
        """Local usado para dar fulfillment nos pedidos ja finalizados.

        Exige o escopo `read_locations`. Sem ele a migracao continua: os pedidos
        entram sem fulfillment e podem ser despachados depois pelo admin.
        """
        try:
            data = self.execute(PRIMARY_LOCATION_QUERY)
        except ShopifyError as exc:
            self.log(f"[aviso] nao foi possivel listar locations ({exc}).")
            self.log("        Adicione o escopo `read_locations` ao app e reinstale "
                    "para que pedidos FINALIZADO entrem ja despachados.")
            return None
        nodes = (data.get("locations") or {}).get("nodes") or []
        for node in nodes:
            if node.get("isActive") and node.get("fulfillsOnlineOrders"):
                return node["id"]
        for node in nodes:
            if node.get("isActive"):
                return node["id"]
        return nodes[0]["id"] if nodes else None

    def detect_order_read_access(self) -> bool:
        """Descobre se o app pode LER o objeto Order.

        Apps sem aprovacao de Protected Customer Data conseguem criar pedidos,
        mas qualquer leitura do objeto Order e negada - inclusive o `order`
        devolvido pelo proprio `orderCreate`. Detectar isso antes do primeiro
        envio evita o pior caso: a mutation cria o pedido, a leitura falha, e o
        script conta como erro e recria tudo na proxima rodada.
        """
        try:
            self.execute(ORDER_READ_PROBE_QUERY)
            self.can_read_orders = True
        except ShopifyError as exc:
            if "protected-customer-data" in str(exc) or "ACCESS_DENIED" in str(exc):
                self.can_read_orders = False
            else:
                raise
        return self.can_read_orders

    def order_count(self, query: str | None = None) -> int:
        """Conta pedidos. Dado agregado: funciona sem Protected Customer Data."""
        data = self.execute(ORDERS_COUNT_QUERY, {"query": query})
        return int(((data.get("ordersCount") or {}).get("count")) or 0)

    def order_exists(self, bagy_id: str) -> bool:
        """Confere pela tag se o pedido da Bagy ja existe na loja."""
        return self.order_count(f"tag:bagy-id-{bagy_id}") > 0

    def find_order_by_bagy_id(self, bagy_id: str) -> dict | None:
        """Busca o pedido pela tag e traz os fulfillment orders.

        Exige acesso de leitura ao objeto Order (Protected Customer Data).
        """
        data = self.execute(FIND_ORDER_BY_TAG_QUERY, {"query": f"tag:bagy-id-{bagy_id}"})
        nodes = (data.get("orders") or {}).get("nodes") or []
        return nodes[0] if nodes else None

    def ensure_customer_document_definition(self) -> str:
        """Garante a definicao do metafield CPF/CNPJ do cliente, fixada no admin.

        Idempotente: se ja existe, a Shopify devolve userError TAKEN e o metodo
        segue em frente. Sem a definicao o valor ate e gravado, mas o admin nao
        mostra na pagina do cliente.
        """
        definition = {
            "ownerType": "CUSTOMER",
            "namespace": CUSTOMER_DOCUMENT_NAMESPACE,
            "key": CUSTOMER_DOCUMENT_KEY,
            "name": "CPF/CNPJ",
            "description": "Documento fiscal do cliente, importado da Bagy",
            "type": "single_line_text_field",
            "pin": True,
            # `access` fica no padrao: definir admin: MERCHANT_READ_WRITE aqui e
            # recusado ("must be one of [public_read_write]") para o namespace
            # `custom` neste app.
        }
        data = self.execute(METAFIELD_DEFINITION_CREATE_MUTATION, {"definition": definition})
        result = data.get("metafieldDefinitionCreate") or {}

        for error in result.get("userErrors") or []:
            if error.get("code") == "TAKEN":
                return "ja existia"
            raise ShopifyUserError(result["userErrors"], request_id=self.last_request_id)

        created = result.get("createdDefinition") or {}
        return f"criada ({created.get('id')})"

    def set_customer_document(self, customer_gid: str, document: str) -> dict | None:
        """Grava o CPF/CNPJ como metafield do cliente."""
        data = self.execute(METAFIELDS_SET_MUTATION, {"metafields": [{
            "ownerId": customer_gid,
            "namespace": CUSTOMER_DOCUMENT_NAMESPACE,
            "key": CUSTOMER_DOCUMENT_KEY,
            "type": "single_line_text_field",
            "value": document,
        }]})
        result = data.get("metafieldsSet") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            raise ShopifyUserError(user_errors, request_id=self.last_request_id)
        metafields = result.get("metafields") or []
        return metafields[0] if metafields else None

    def get_order_by_gid(self, gid: str) -> dict | None:
        """Le o pedido pelo GID, com os fulfillment orders. Sem atraso de indice."""
        return self.execute(ORDER_BY_GID_QUERY, {"id": gid}).get("order")

    def update_order(self, order_input: dict) -> dict:
        """Aplica no pedido o que o `orderCreate` nao aceita.

        Principal uso: `localizedFields` com o CPF/CNPJ (TAX_CREDENTIAL_BR),
        que so existe em `OrderInput`. Tambem serve para corrigir pedidos ja
        criados, porque `customAttributes` aqui sobrescreve a lista inteira.
        """
        data = self.execute(ORDER_UPDATE_MUTATION, {"input": order_input}, throttled=False)
        result = data.get("orderUpdate") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            raise ShopifyUserError(user_errors, request_id=self.last_request_id)
        order = result.get("order")
        if not order:
            raise ShopifyError("orderUpdate nao devolveu pedido nem userErrors")
        return order

    def create_fulfillment(self, fulfillment_input: dict) -> dict:
        data = self.execute(
            FULFILLMENT_CREATE_MUTATION, {"fulfillment": fulfillment_input}, throttled=True
        )
        result = data.get("fulfillmentCreate") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            raise ShopifyUserError(user_errors, request_id=self.last_request_id)
        fulfillment = result.get("fulfillment")
        if not fulfillment:
            raise ShopifyError("fulfillmentCreate nao devolveu fulfillment nem userErrors")
        return fulfillment

    def create_order(self, order_input: dict, options: dict) -> dict:
        """Cria o pedido e devolve o que foi possivel ler de volta.

        Sem acesso de leitura a Order, usa a mutation minima (so `userErrors`):
        o pedido e criado normalmente, mas sem GID/nome na resposta.
        """
        variables = {"order": order_input, "options": options}

        if not self.can_read_orders:
            data = self.execute(ORDER_CREATE_MINIMAL_MUTATION, variables, throttled=True)
            result = data.get("orderCreate") or {}
            user_errors = result.get("userErrors") or []
            if user_errors:
                raise ShopifyUserError(user_errors, request_id=self.last_request_id)
            return {"id": None, "name": None, "readable": False}

        data = self.execute(ORDER_CREATE_MUTATION, variables, throttled=True)
        result = data.get("orderCreate") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            raise ShopifyUserError(user_errors, request_id=self.last_request_id)
        order = result.get("order")
        if not order:
            raise ShopifyError("orderCreate nao devolveu pedido nem userErrors")
        order["readable"] = True
        return order
