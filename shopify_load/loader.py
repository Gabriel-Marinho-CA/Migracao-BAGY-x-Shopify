"""Envio dos payloads: mutation principal, refs e pos-acoes, item a item."""

from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict

from bagy2shopify.fix_fulfillment import pending_fulfillment_orders
from bagy2shopify.shopify_client import ShopifyError, ShopifyUserError
from bagy_transform.common import REF_RE, SELF, compact
from bagy_transform.mutations import DOCUMENTS
from shopify_snapshot.diff import metafield_value

# Ordem de carga: cada entidade so depende das anteriores.
LOAD_ORDER = ["metaobject_definition", "metafield_definition", "policy", "blog", "page", "collection", "file",
              "metaobject", "product", "product_content", "customer", "lead", "store_credit", "order", "discount",
              "article", "menu", "redirect"]

UPSERT_MUTATIONS = {"productSet", "customerSet", "shopPolicyUpdate", "metaobjectUpsert", "metafieldsSet"}
# Ja carregado e com o payload mudado: reenvia (atualiza no lugar, sem duplicar).
# productSet fica de fora de proposito: reenviar arquivos duplicaria imagens.
RESEND_ON_CHANGE = {"metaobjectUpsert", "metafieldsSet"}

# Onde vem o ID criado, na resposta de cada mutation principal (lista: o primeiro).
ID_PATHS = {
    "metaobjectDefinitionCreate": "metaobjectDefinition",
    "metaobjectUpsert": "metaobject",
    "fileCreate": "files",
    "metafieldDefinitionCreate": "createdDefinition",
    "collectionCreate": "collection",
    "productSet": "product",
    "customerSet": "customer",
    "discountCodeBasicCreate": "codeDiscountNode",
    "discountCodeFreeShippingCreate": "codeDiscountNode",
    "blogCreate": "blog",
    "articleCreate": "article",
    "pageCreate": "page",
    "shopPolicyUpdate": "shopPolicy",
    "menuCreate": "menu",
    "urlRedirectCreate": "urlRedirect",
    "orderCreate": "order",
}

# productSet com o metafield da variacao na resposta: e assim que a variante
# criada e ligada a `bagy-ref:variant:<id>` que os pedidos usam.
PRODUCT_SET = """mutation ProductSet($identifier: ProductSetIdentifiers, $input: ProductSetInput!, $synchronous: Boolean) {
  productSet(identifier: $identifier, input: $input, synchronous: $synchronous) {
    product {
      id
      handle
      variants(first: 100) {
        nodes {
          id
          sku
          metafield(namespace: "bagy", key: "variation_id") {
            value
          }
        }
      }
    }
    productSetOperation {
      id
      status
    }
    userErrors {
      field
      message
      code
    }
  }
}"""


def document(mutation: str) -> str:
    return PRODUCT_SET if mutation == "productSet" else DOCUMENTS[mutation]


def resolve(value, refs: dict, self_id: str | None = None) -> tuple:
    """Troca `bagy-ref:*` pelos IDs da Shopify. Devolve (valor, refs que faltaram)."""
    missing = set()

    def replace(match):
        found = match.group(0)
        if found in refs:
            return refs[found]
        missing.add(found)
        return found

    def walk(node):
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            if node == SELF:
                if self_id:
                    return self_id
                missing.add(SELF)
                return node
            return REF_RE.sub(replace, node)
        return node

    return walk(value), sorted(missing)


def maybe_executed(exc: ShopifyError) -> bool:
    """A mutation pode ter rodado mesmo com erro?

    Sim para rede/5xx e para leitura negada num campo DA RESPOSTA (path com mais
    de um nivel, ex. orderCreate.order.id). Acesso negado no proprio campo raiz
    (falta o escopo da mutation) significa que ela nem rodou.
    """
    if exc.status is None or exc.status >= 500:
        return True
    for error in exc.graphql_errors or []:
        if not isinstance(error, dict):
            continue
        code = str((error.get("extensions") or {}).get("code") or "").upper()
        if code == "ACCESS_DENIED" and len(error.get("path") or []) > 1:
            return True
    return False


def short(exc) -> str:
    return " ".join(str(exc).split())[:300]


def ref_kinds(refs) -> str:
    return ", ".join(sorted({ref.split(":")[1] if ref.count(":") >= 2 else ref for ref in refs}))


class Loader:
    def __init__(self, client, store, *, product_images: bool = False, dry_run: bool = False, errors=None,
                 run_id: int | None = None, log=print):
        self.client = client
        self.store = store
        self.product_images = product_images
        self.dry_run = dry_run
        self.errors = errors
        self.run_id = run_id
        self.log = log
        self.refs = store.refs()
        self.existing = store.existing()
        self.state = store.items()
        self.results = defaultdict(Counter)
        self.touched = []   # (entity, source_key, resultado, shopify_id)

    # ----------------------------------------------------------------- #

    def run(self, plan: dict) -> dict:
        for entity in LOAD_ORDER:
            items = plan.get(entity) or []
            if not items:
                continue
            self.log(f"\n[{entity}] {len(items)} item(ns)")
            for item in items:
                result, detail = self.load_item(item)
                self.results[entity][result] += 1
                self.state = self.store.items() if not self.dry_run else self.state
                self.touched.append((entity, item.source_key, result, detail))
                self.log(f"  [{result:<12}] {item.source_key}" + (f"  {detail}" if detail else ""))
        return self.results

    def _mark(self, item, status: str, **fields) -> None:
        if not self.dry_run:
            self.store.mark(item.entity, item.source_key, status, payload_hash=item.hash, run_id=self.run_id,
                            **fields)

    def _register(self, item, mapping: dict) -> None:
        self.refs.update(mapping)
        if mapping and not self.dry_run:
            self.store.add_refs(mapping, item.entity, item.source_key)

    def _record(self, item, stage: str, exc, payload) -> None:
        if self.errors is not None:
            self.errors.record(script="load", stage=stage, bagy_id=item.source_key, message=short(exc), error=exc,
                               payload=payload, context={"entity": item.entity})

    def _block(self, item, reason: str) -> tuple:
        self._mark(item, "blocked", error=reason)
        return "bloqueado", reason

    # ----------------------------------------------------------------- #

    def load_item(self, item) -> tuple:
        key = (item.entity, item.source_key)
        state = self.state.get(key) or {}
        status = state.get("status")
        changed = (status == "done" and item.mutation in RESEND_ON_CHANGE
                   and state.get("payload_hash") and state["payload_hash"] != item.hash)
        if status in ("done", "existing") and not changed:
            return "ja carregado", state.get("shopify_id")
        if status in ("uncertain", "sending"):
            return "incerto", "pode ter sido criado sem resposta: rode o snapshot antes de tentar de novo"

        diff_row = self.existing.get(key)
        if not diff_row:
            return self._block(item, "sem diff: rode run_shopify_snapshot.py")
        if diff_row.get("payload_hash") != item.hash:
            return self._block(item, "diff desatualizado: rode run_shopify_snapshot.py --diff-only")
        action = diff_row["action"]
        if action == "skip":
            self._mark(item, "existing", shopify_id=diff_row.get("shopify_id"), error=None)
            return "ja existia", diff_row.get("shopify_id")
        if action not in ("create", "update"):
            return self._block(item, f"diff marcou {action}")
        label = "atualizado" if action == "update" else "criado"

        if status == "partial" and state.get("shopify_id") is not None:
            shopify_id = state["shopify_id"]
            steps = set(json.loads(state.get("steps") or "[]"))
            warnings = json.loads(state.get("warnings") or "[]")
        elif status == "partial" and not ID_PATHS.get(item.mutation):
            shopify_id, steps, warnings = None, set(json.loads(state.get("steps") or "[]")), []
        else:
            outcome = self._main(item, action, diff_row)
            if outcome[0] != "ok":
                return outcome[1], outcome[2]
            shopify_id, steps, warnings = outcome[1], set(), list(outcome[2])
            if self.dry_run:
                extra = f" + {len(item.post_actions)} pos-acao(oes)" if item.post_actions else ""
                return "enviaria", f"{item.mutation}{extra}"

        for index, action_ in enumerate(item.post_actions):
            if index in steps:
                continue
            ok, message = self._post(item, action_, shopify_id)
            if not ok:
                self._mark(item, "partial", shopify_id=shopify_id, steps=sorted(steps), warnings=warnings,
                           error=message)
                return "parcial", message
            if message:
                warnings.append(message)
            steps.add(index)
            self._mark(item, "partial", shopify_id=shopify_id, steps=sorted(steps), warnings=warnings, error=None)

        self._mark(item, "done", shopify_id=shopify_id, steps=sorted(steps), warnings=warnings, error=None)
        detail = shopify_id or ""
        if warnings:
            detail += ("  " if detail else "") + "aviso: " + "; ".join(warnings)
        return label, detail

    def _main(self, item, action: str, diff_row: dict) -> tuple:
        variables = copy.deepcopy(item.variables)
        if action == "update" and item.mutation in ("productSet", "customerSet") and diff_row.get("shopify_id"):
            variables["identifier"] = {"id": diff_row["shopify_id"]}
        if item.mutation == "productSet" and not self.product_images:
            (variables.get("input") or {}).pop("files", None)
        variables, missing = resolve(variables, self.refs)
        if missing:
            return ("stop",) + self._block(item, "referencia ainda nao carregada: " + ref_kinds(missing))

        if self.dry_run:
            fake = f"gid://dry-run/{item.entity}/{item.source_key}"
            self._register(item, {ref: fake for ref in item.provides})
            return "ok", fake, []

        self._mark(item, "sending", attempt=True, error=None)
        try:
            data = self.client.execute(document(item.mutation), variables,
                                       throttled=item.mutation == "orderCreate")
        except ShopifyError as exc:
            if exc.status == 401:
                self._mark(item, "failed", error=short(exc))
                raise
            self._record(item, item.mutation, exc, variables)
            uncertain = item.mutation not in UPSERT_MUTATIONS and maybe_executed(exc)
            self._mark(item, "uncertain" if uncertain else "failed", error=short(exc))
            return "stop", "incerto" if uncertain else "falhou", short(exc)

        result = data.get(item.mutation) or {}
        user_errors = result.get("userErrors") or result.get("orderCancelUserErrors") or []
        if user_errors:
            if item.mutation == "metafieldDefinitionCreate" and all(e.get("code") == "TAKEN" for e in user_errors):
                self._mark(item, "existing", error=None)
                return "stop", "ja existia", "definicao ja existia na loja"
            exc = ShopifyUserError(user_errors, request_id=self.client.last_request_id)
            self._record(item, item.mutation, exc, variables)
            self._mark(item, "failed", error=short(exc))
            return "stop", "falhou", short(exc)

        shopify_id = None
        if ID_PATHS.get(item.mutation):
            node = result.get(ID_PATHS[item.mutation])
            if isinstance(node, list):
                node = node[0] if node else None
            shopify_id = (node or {}).get("id")
            if not shopify_id:
                exc = ShopifyError(f"{item.mutation} nao devolveu ID nem userErrors")
                self._record(item, item.mutation, exc, variables)
                self._mark(item, "uncertain", error=short(exc))
                return "stop", "incerto", short(exc)

        mapping = self._refs_for(item, result, shopify_id)
        self._register(item, mapping)
        warnings = []
        unmapped = [ref for ref in item.provides if ref not in mapping]
        if unmapped:
            warnings.append("sem ID para " + ref_kinds(unmapped))
        self._mark(item, "partial", shopify_id=shopify_id, steps=[], warnings=warnings, error=None)
        return "ok", shopify_id, warnings

    def _refs_for(self, item, result: dict, shopify_id: str | None) -> dict:
        if item.mutation != "productSet":
            return {ref: shopify_id for ref in item.provides} if shopify_id else {}
        product = result.get("product") or {}
        variants = (product.get("variants") or {}).get("nodes") or []
        payload_variants = (item.variables.get("input") or {}).get("variants") or []
        mapping = {}
        for ref in item.provides:
            if ref.startswith("bagy-ref:product:"):
                mapping[ref] = shopify_id
                continue
            if not ref.startswith("bagy-ref:variant:"):
                continue
            variation = ref.rsplit(":", 1)[1]
            node = next((v for v in variants if str((v.get("metafield") or {}).get("value") or "") == variation), None)
            if node is None:
                skus = {str(v.get("sku") or "").strip().upper() for v in payload_variants
                        if str(metafield_value(v.get("metafields"), "bagy", "variation_id")) == variation}
                skus.discard("")
                node = next((v for v in variants if str(v.get("sku") or "").strip().upper() in skus), None)
            if node is None and len(variants) == 1 and len(payload_variants) == 1:
                node = variants[0]
            if node:
                mapping[ref] = node["id"]
        return mapping

    # ----------------------------------------------------------------- #

    def _post(self, item, action: dict, shopify_id: str | None) -> tuple:
        """Devolve (ok, mensagem). ok=True com mensagem = seguiu com aviso."""
        if action.get("action") == "fulfillment_sweep":
            return self._sweep(item, shopify_id)
        mutation = action.get("mutation")
        variables, missing = resolve(copy.deepcopy(action.get("variables") or {}), self.refs, self_id=shopify_id)
        if missing:
            return False, f"{mutation}: referencia pendente ({ref_kinds(missing)})"
        try:
            data = self.client.execute(DOCUMENTS[mutation], variables)
        except ShopifyError as exc:
            if exc.status == 401:
                raise
            self._record(item, mutation, exc, variables)
            return False, f"{mutation}: {short(exc)}"
        result = data.get(mutation) or {}
        user_errors = result.get("userErrors") or result.get("orderCancelUserErrors") or []
        if not user_errors:
            return True, None
        exc = ShopifyUserError(user_errors, request_id=self.client.last_request_id)
        self._record(item, mutation, exc, variables)
        if mutation == "orderUpdate":
            # A Shopify valida o documento: recusado, fica so nas informacoes adicionais.
            return True, f"CPF/CNPJ recusado pela Shopify ({short(exc)[:120]})"
        return False, f"{mutation}: {short(exc)}"

    def _sweep(self, item, shopify_id: str | None) -> tuple:
        """Despacha os fulfillment orders que o `fulfillment` do orderCreate nao cobriu."""
        if not shopify_id:
            return False, "varredura de fulfillment sem ID do pedido"
        try:
            order = self.client.get_order_by_gid(shopify_id)
            if not order:
                return False, "varredura de fulfillment: pedido nao encontrado pelo ID"
            pending = pending_fulfillment_orders(order)
            if not pending:
                return True, None
            fulfillment = (item.variables.get("order") or {}).get("fulfillment") or {}
            payload = {"notifyCustomer": False,
                       "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo["id"]} for fo in pending]}
            tracking = compact({"number": fulfillment.get("trackingNumber"),
                                "company": fulfillment.get("trackingCompany")})
            if tracking.get("number"):
                payload["trackingInfo"] = tracking
            self.client.create_fulfillment(payload)
            return True, None
        except (ShopifyUserError, ShopifyError) as exc:
            if getattr(exc, "status", None) == 401:
                raise
            self._record(item, "fulfillment_sweep", exc, None)
            return False, f"varredura de fulfillment: {short(exc)}"
