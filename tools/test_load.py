"""Testes da carga, sem rede: cliente Shopify falso e payloads sinteticos.

Uso: python tools/test_load.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bagy2shopify.shopify_client import ShopifyError
from bagy_transform.model import Payload
from bagy_transform.store import TransformStore
from shopify_load import pilot
from shopify_load.loader import PRODUCT_SET, Loader, resolve
from shopify_load.media import MEDIA_ADD, MEDIA_STATUS, MediaBackfill
from shopify_load.store import LoadStore
from shopify_snapshot import diff
from shopify_snapshot.diff import load_items
from shopify_snapshot.storage import SnapshotStore

FAILURES = 0


def check(description: str, condition, detail=None) -> None:
    global FAILURES
    print(f"  [{'ok ' if condition else 'FALHOU'}] {description}")
    if not condition:
        FAILURES += 1
        if detail is not None:
            print(f"         {detail}")


LOCATION = "gid://shopify/Location/9"
PUBLICATION = "gid://shopify/Publication/7"


def payloads() -> dict:
    return {
        "metafield_definition": [Payload("metafield_definition", "def:a", "metafieldDefinitionCreate", variables={
            "definition": {"ownerType": "PRODUCT", "namespace": "custom", "key": "a", "type": "single_line_text_field",
                           "name": "A"}}, provides=["bagy-ref:metafield_definition:a"])],
        "page": [Payload("page", "hotsite:1", "pageCreate", variables={"page": {"title": "Hot", "handle": "hot"}},
                         provides=["bagy-ref:page:page:1"])],
        "collection": [Payload("collection", "category:5", "collectionCreate", variables={
            "collection": {"title": "Kits", "handle": "kits"}},
            post_actions=[{"mutation": "publishablePublish", "variables": {
                "id": "bagy-ref:self", "input": [{"publicationId": "bagy-ref:publication:online_store"}]}}],
            provides=["bagy-ref:collection:category:5"])],
        "product": [Payload("product", "product:1", "productSet", variables={
            "identifier": {"handle": "p1"}, "synchronous": True,
            "input": {"title": "P1", "handle": "p1", "collections": ["bagy-ref:collection:category:5"],
                      "files": [{"originalSource": "https://cdn/x.png", "contentType": "IMAGE"}],
                      "variants": [{"sku": "S1", "inventoryQuantities": [
                          {"locationId": "bagy-ref:location:default", "name": "available", "quantity": 3}],
                          "metafields": [{"namespace": "bagy", "key": "variation_id", "type": "number_integer",
                                          "value": "11"}]}],
                      "metafields": [{"namespace": "custom", "key": "conteudo_pagina", "type": "page_reference",
                                      "value": "bagy-ref:page:page:1"}]}},
            post_actions=[{"mutation": "publishablePublish", "variables": {
                "id": "bagy-ref:self", "input": [{"publicationId": "bagy-ref:publication:online_store"}]}}],
            provides=["bagy-ref:product:1", "bagy-ref:variant:11"])],
        "customer": [Payload("customer", "customer:100", "customerSet", variables={
            "identifier": {"email": "a@b.com"}, "input": {"email": "a@b.com"}},
            post_actions=[
                {"mutation": "metafieldsSet", "variables": {"metafields": [
                    {"ownerId": "bagy-ref:self", "namespace": "bagy", "key": "customer_id", "type": "number_integer",
                     "value": "100"}]}},
                {"mutation": "customerEmailMarketingConsentUpdate", "variables": {"input": {
                    "customerId": "bagy-ref:self", "emailMarketingConsent": {"marketingState": "SUBSCRIBED"}}}}],
            provides=["bagy-ref:customer:100"])],
        "store_credit": [Payload("store_credit", "cashback:1", "storeCreditAccountCredit", variables={
            "id": "bagy-ref:customer:100", "creditInput": {"creditAmount": {"amount": "5.00", "currencyCode": "BRL"}}})],
        "order": [
            Payload("order", "order:1", "orderCreate", variables={"order": {
                "name": "#17300000000001", "financialStatus": "PAID", "sourceIdentifier": "bagy-1",
                "processedAt": "2025-01-10T10:00:00-03:00",
                "customer": {"toAssociate": {"id": "bagy-ref:customer:100"}},
                "lineItems": [{"title": "P1", "quantity": 1, "variantId": "bagy-ref:variant:11"}],
                "fulfillment": {"locationId": "bagy-ref:location:default", "trackingNumber": "BR1",
                                "trackingCompany": "Correios"}}},
                post_actions=[{"mutation": "orderUpdate", "variables": {"input": {
                    "id": "bagy-ref:self", "localizedFields": [{"key": "TAX_CREDENTIAL_BR", "value": "1"}]}}},
                    {"action": "fulfillment_sweep", "notifyCustomer": False}],
                provides=["bagy-ref:order:1"], meta={"total_shopify": "10.00"}),
            Payload("order", "order:2", "orderCreate", variables={"order": {
                "name": "#17300000000002", "financialStatus": "VOIDED", "sourceIdentifier": "bagy-2",
                "customer": {"toAssociate": {"id": "bagy-ref:customer:100"}},
                "lineItems": [{"title": "P1", "quantity": 1, "variantId": "bagy-ref:variant:11"}]}},
                post_actions=[{"mutation": "orderCancel", "variables": {
                    "orderId": "bagy-ref:self", "reason": "DECLINED", "restock": False}}],
                provides=["bagy-ref:order:2"], meta={"total_shopify": "10.00"}),
        ],
    }


class FakeClient:
    ROOTS = ("metafieldDefinitionCreate", "pageCreate", "collectionCreate", "productSet", "customerSet",
             "storeCreditAccountCredit", "orderCreate", "metafieldsSet", "customerEmailMarketingConsentUpdate",
             "orderUpdate", "orderCancel", "publishablePublish", "productUpdate", "metaobjectDefinitionCreate",
             "metaobjectUpsert", "fileCreate", "pageDelete", "metafieldDefinitionDelete", "metaobjectDefinitionDelete",
             "metaobjectDelete", "articleUpdate", "article", "node", "product")

    def __init__(self):
        self.calls = []
        self.fail = {}        # root -> exception ou lista de userErrors (uma vez)
        self.fulfillments = []
        self.last_request_id = None
        self.counter = 0
        self.media = {}       # product gid -> lista de nodes de midia
        self.bodies = {}      # article gid -> HTML do texto

    def execute(self, query, variables=None, throttled=False):
        root = next(name for name in self.ROOTS if f"{name}(" in query)
        self.calls.append((root, variables))
        if root == "article":
            return {"article": {"id": variables["id"], "title": "Artigo", "body": self.bodies.get(variables["id"], "")}}
        if root == "articleUpdate":
            self.bodies[variables["id"]] = variables["article"]["body"]
            return {"articleUpdate": {"article": {"id": variables["id"]}, "userErrors": []}}
        if root == "node":
            number = variables["id"].rsplit("/", 1)[1]
            return {"node": {"id": variables["id"], "fileStatus": "READY",
                             "image": {"url": f"https://cdn.shopify.com/s/files/imagem-{number}.webp"}}}
        if root == "product":
            nodes = self.media.get(variables["id"], [])
            return {"product": {"id": variables["id"], "mediaCount": {"count": len(nodes)}, "media": {"nodes": nodes}}}
        if root == "productUpdate":
            gid = variables["product"]["id"]
            self.media.setdefault(gid, []).extend(
                {"id": f"gid://shopify/MediaImage/{i}", "status": "READY", "mediaErrors": []}
                for i, _ in enumerate(variables["media"]))
            return {"productUpdate": {"product": {"id": gid}, "userErrors": []}}
        failure = self.fail.pop(root, None)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            key = "orderCancelUserErrors" if root == "orderCancel" else "userErrors"
            return {root: {key: failure}}
        self.counter += 1
        gid = lambda kind: f"gid://shopify/{kind}/{self.counter}"
        bodies = {
            "metafieldDefinitionCreate": {"createdDefinition": {"id": gid("MetafieldDefinition")}},
            "pageCreate": {"page": {"id": gid("Page")}},
            "collectionCreate": {"collection": {"id": gid("Collection")}},
            "productSet": {"product": {"id": gid("Product"), "variants": {"nodes": [
                {"id": gid("ProductVariant"), "sku": "S1", "metafield": None}]}}},
            "customerSet": {"customer": {"id": gid("Customer")}},
            "storeCreditAccountCredit": {"storeCreditAccountTransaction": {"__typename": "X"}},
            "orderCreate": {"order": {"id": gid("Order"), "name": "#1"}},
            "metaobjectDefinitionCreate": {"metaobjectDefinition": {"id": gid("MetaobjectDefinition"), "type": "selo"}},
            "metaobjectUpsert": {"metaobject": {"id": gid("Metaobject"), "handle": "x"}},
            "fileCreate": {"files": [{"id": gid("MediaImage"), "fileStatus": "UPLOADED"}]},
            "pageDelete": {"deletedPageId": (variables or {}).get("id")},
            "metafieldDefinitionDelete": {"deletedDefinitionId": (variables or {}).get("id")},
            "metaobjectDefinitionDelete": {"deletedId": (variables or {}).get("id")},
            "metaobjectDelete": {"deletedId": (variables or {}).get("id")},
        }
        return {root: {**bodies.get(root, {}), "userErrors": []}}

    def get_order_by_gid(self, gid):
        return {"id": gid, "fulfillmentOrders": {"nodes": [
            {"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN",
             "lineItems": {"nodes": [{"id": "x", "remainingQuantity": 1}]}}]}}

    def create_fulfillment(self, payload):
        self.fulfillments.append(payload)
        return {"status": "SUCCESS", "totalQuantity": 1}


def setup(tmp: Path) -> tuple:
    transform_db = tmp / "shopify.sqlite"
    store = TransformStore(transform_db)
    run_id = store.start_run("2026-09-13", ["x"])
    for entity, items in payloads().items():
        store.replace(entity, items, run_id)
    store.close()
    snapshot = SnapshotStore(tmp / "snap.sqlite")
    for resource in ("shop", "policies", "metafield_definitions", "collections", "products", "customers",
                     "discounts", "blogs", "articles", "pages", "menus", "url_redirects", "orders"):
        snapshot.replace(resource, [], 1)
    snapshot.replace("locations", [{"id": LOCATION, "is_active": True, "is_primary": True}], 1)
    snapshot.replace("publications", [{"id": PUBLICATION, "name": "Online Store"}], 1)
    snapshot.set_meta("access_scopes", ["read_all_orders"])
    diff.run(transform_db, snapshot)
    snapshot.close()
    load_store = LoadStore(transform_db)
    return load_store, load_items(load_store.conn)


def plan_all(grouped) -> dict:
    return {entity: list(items) for entity, items in grouped.items()}


def calls_of(client, root) -> list:
    return [variables for name, variables in client.calls if name == root]


def test_resolve() -> None:
    print("\nResolucao de referencias")
    value, missing = resolve({"a": "bagy-ref:product:1", "b": ["bagy-ref:self", "x"], "c": '["bagy-ref:page:page:1"]'},
                             {"bagy-ref:product:1": "gid://P/1", "bagy-ref:page:page:1": "gid://Pg/1"}, self_id="gid://S/1")
    check("troca refs em valor, lista, JSON dentro de string e self",
          value == {"a": "gid://P/1", "b": ["gid://S/1", "x"], "c": '["gid://Pg/1"]'} and not missing, value)
    _, missing = resolve({"a": "bagy-ref:customer:9"}, {})
    check("aponta a ref que falta", missing == ["bagy-ref:customer:9"])


def test_dry_run(tmp: Path) -> None:
    print("\nDry-run")
    store, grouped = setup(tmp)
    client = FakeClient()
    loader = Loader(client, store, dry_run=True, log=lambda *_: None)
    results = loader.run(plan_all(grouped))
    check("nada enviado", client.calls == [])
    check("tudo resolvido na ordem (pedidos enxergam cliente e variante do dry-run)",
          results["order"]["enviaria"] == 2 and sum(r["bloqueado"] for r in results.values()) == 0, dict(results))
    check("dry-run nao grava estado", store.items() == {})
    store.close()


def test_pilot(tmp: Path) -> None:
    print("\nSelecao do piloto")
    store, grouped = setup(tmp)
    selection = pilot.select(grouped, store.existing(), max_orders=10)
    check("pega os dois pedidos (casos diferentes)", selection.get("order") == ["order:1", "order:2"], selection)
    check("traz as dependencias: cliente, produto, colecao, pagina, definicoes, credito",
          selection.get("customer") == ["customer:100"] and selection.get("product") == ["product:1"]
          and selection.get("collection") == ["category:5"] and selection.get("page") == ["hotsite:1"]
          and selection.get("metafield_definition") == ["def:a"] and selection.get("store_credit") == ["cashback:1"],
          selection)
    check("lista os casos cobertos", "cancelado" in selection["_casos_cobertos"]
          and "enviado com rastreio" in selection["_casos_cobertos"], selection["_casos_cobertos"])
    store.close()


def test_load(tmp: Path) -> None:
    print("\nCarga completa com cliente falso")
    store, grouped = setup(tmp)
    client = FakeClient()
    client.fail["orderUpdate"] = [{"field": ["localizedFields"], "message": "Tax credential is invalid"}]
    results = Loader(client, store, log=lambda *_: None).run(plan_all(grouped))
    order_roots = [name for name, _ in client.calls if name in FakeClient.ROOTS[:7]]
    check("ordem: definicao, pagina, colecao, produto, cliente, credito, pedidos",
          order_roots == ["metafieldDefinitionCreate", "pageCreate", "collectionCreate", "productSet", "customerSet",
                          "storeCreditAccountCredit", "orderCreate", "orderCreate"], order_roots)
    product = calls_of(client, "productSet")[0]
    check("produto sem imagens (files removido)", "files" not in product["input"])
    check("produto com colecao, pagina e local resolvidos",
          product["input"]["collections"][0].startswith("gid://shopify/Collection/")
          and product["input"]["metafields"][0]["value"].startswith("gid://shopify/Page/")
          and product["input"]["variants"][0]["inventoryQuantities"][0]["locationId"] == LOCATION, product)
    publishes = calls_of(client, "publishablePublish")
    check("publica colecao e produto no canal do snapshot",
          len(publishes) == 2 and all(p["input"][0]["publicationId"] == PUBLICATION for p in publishes), publishes)
    customer_id = calls_of(client, "metafieldsSet")[0]["metafields"][0]["ownerId"]
    check("pos-acoes do cliente usam o ID criado", customer_id.startswith("gid://shopify/Customer/")
          and calls_of(client, "customerEmailMarketingConsentUpdate")[0]["input"]["customerId"] == customer_id)
    check("credito vai para o cliente criado", calls_of(client, "storeCreditAccountCredit")[0]["id"] == customer_id)
    first_order = calls_of(client, "orderCreate")[0]["order"]
    check("pedido com cliente, variante e local resolvidos",
          first_order["customer"]["toAssociate"]["id"] == customer_id
          and first_order["lineItems"][0]["variantId"].startswith("gid://shopify/ProductVariant/")
          and first_order["fulfillment"]["locationId"] == LOCATION, first_order)
    check("CPF recusado vira aviso e o pedido segue", results["order"]["criado"] == 2
          and "CPF/CNPJ recusado" in (store.items()[("order", "order:1")]["warnings"] or ""), dict(results["order"]))
    check("varredura de fulfillment despacha com rastreio", client.fulfillments
          and client.fulfillments[0]["trackingInfo"] == {"number": "BR1", "company": "Correios"}, client.fulfillments)
    check("cancelamento usa o ID do pedido", calls_of(client, "orderCancel")[0]["orderId"].startswith("gid://shopify/Order/"))

    client2 = FakeClient()
    results = Loader(client2, store, log=lambda *_: None).run(plan_all(grouped))
    check("rodar de novo nao envia nada", client2.calls == [] and results["order"]["ja carregado"] == 2, dict(results))
    store.close()


def test_resume_and_uncertain(tmp: Path) -> None:
    print("\nRetomada, incerteza e bloqueio")
    store, grouped = setup(tmp)
    client = FakeClient()
    client.fail["customerEmailMarketingConsentUpdate"] = [{"field": ["input"], "message": "erro qualquer"}]
    client.fail["orderCreate"] = ShopifyError("leitura negada", status=200, graphql_errors=[
        {"extensions": {"code": "ACCESS_DENIED"}, "path": ["orderCreate", "order", "id"]}])
    client.fail["storeCreditAccountCredit"] = ShopifyError("sem escopo", status=200, graphql_errors=[
        {"extensions": {"code": "ACCESS_DENIED"}, "path": ["storeCreditAccountCredit"]}])
    results = Loader(client, store, log=lambda *_: None).run(plan_all(grouped))
    check("escopo negado no campo raiz: falhou (nao rodou), pode tentar de novo",
          results["store_credit"]["falhou"] == 1, dict(results["store_credit"]))
    check("pos-acao recusada deixa o cliente parcial", results["customer"]["parcial"] == 1, dict(results["customer"]))
    check("pedido com leitura negada na resposta fica incerto", results["order"]["incerto"] >= 1, dict(results["order"]))

    client2 = FakeClient()
    results = Loader(client2, store, log=lambda *_: None).run(plan_all(grouped))
    roots = [name for name, _ in client2.calls]
    check("retomada: so a pos-acao que faltou, sem recriar o cliente",
          "customerSet" not in roots and roots.count("customerEmailMarketingConsentUpdate") == 1, roots)
    check("pedido incerto nao e reenviado", results["order"]["incerto"] == 1, dict(results["order"]))
    store.close()

    tmp2 = tmp / "bloqueio"
    tmp2.mkdir()
    store, grouped = setup(tmp2)
    client = FakeClient()
    client.fail["productSet"] = [{"field": ["input"], "message": "handle invalido"}]
    results = Loader(client, store, log=lambda *_: None).run(plan_all(grouped))
    check("produto recusado: pedidos bloqueados por referencia, sem enviar",
          results["product"]["falhou"] == 1 and results["order"]["bloqueado"] == 2
          and not calls_of(client, "orderCreate"), dict(results))
    check("motivo do bloqueio aponta a variante", "variant" in (store.items()[("order", "order:1")]["error"] or ""))

    with store.conn:
        store.conn.execute("UPDATE existing SET action = 'update', shopify_id = 'gid://shopify/Product/77' "
                           "WHERE entity = 'product'")
    loader = Loader(FakeClient(), store, log=lambda *_: None)
    loader.run({"product": grouped["product"]})
    sent = calls_of(loader.client, "productSet")[0]
    check("diff 'atualizar': productSet pelo ID existente", sent["identifier"] == {"id": "gid://shopify/Product/77"}, sent)
    with store.conn:
        store.conn.execute("UPDATE existing SET payload_hash = 'velho' WHERE entity = 'collection'")
    loader = Loader(FakeClient(), store, log=lambda *_: None)
    results = loader.run({"collection": grouped["collection"]})
    check("diff desatualizado bloqueia", results["collection"]["ja carregado"] + results["collection"]["bloqueado"] == 1)
    store.close()


def test_media(tmp: Path) -> None:
    print("\nImagens dos produtos")
    store, grouped = setup(tmp)
    client = FakeClient()
    Loader(client, store, product_images=True, log=lambda *_: None).run({"page": grouped["page"],
                                                                        "collection": grouped["collection"],
                                                                        "product": grouped["product"]})
    check("com LOAD_PRODUCT_IMAGES=1 o productSet leva os arquivos",
          calls_of(client, "productSet")[0]["input"].get("files", [{}])[0].get("originalSource") == "https://cdn/x.png")
    store.close()

    tmp2 = tmp / "backfill"
    tmp2.mkdir()
    store, grouped = setup(tmp2)
    client = FakeClient()
    Loader(client, store, log=lambda *_: None).run({"page": grouped["page"], "collection": grouped["collection"],
                                                   "product": grouped["product"]})
    products = grouped["product"]
    dry = MediaBackfill(client, store, dry_run=True, log=lambda *_: None)
    check("dry-run: le a loja e nao envia", dry.run(products)["enviaria"] == 1 and not calls_of(client, "productUpdate"))
    backfill = MediaBackfill(client, store, log=lambda *_: None, poll_seconds=0)
    results = backfill.run(products)
    sent = calls_of(client, "productUpdate")
    check("produto sem imagem recebe as imagens pelo productUpdate(media:)",
          results["enviado"] == 1 and sent and sent[0]["media"] == [
              {"originalSource": "https://cdn/x.png", "mediaContentType": "IMAGE"}], sent)
    results = MediaBackfill(client, store, log=lambda *_: None).run(products)
    check("rodar de novo nao reenvia", results["ja carregado"] == 1 and len(calls_of(client, "productUpdate")) == 1)
    with store.conn:
        store.conn.execute("DELETE FROM load_items WHERE entity = 'product_media'")
    results = MediaBackfill(client, store, log=lambda *_: None).run(products)
    check("produto que ja tem as imagens na loja: so marca, sem enviar",
          results["ja tinha"] == 1 and len(calls_of(client, "productUpdate")) == 1, dict(results))
    with store.conn:
        store.conn.execute("DELETE FROM load_items WHERE entity = 'product_media'")
    gid = store.items()[("product", "product:1")]["shopify_id"]
    client.media[gid] = [{"id": "m1", "status": "FAILED", "mediaErrors": [{"code": "IMAGE_DOWNLOAD_FAILURE",
                                                                          "message": "download falhou"}]}]
    results = MediaBackfill(client, store, log=lambda *_: None).run(products)
    check("imagem recusada na Shopify: fica para revisar, com o motivo",
          results["recusada"] == 1 and "download falhou" in (store.items()[("product_media", "product:1")]["error"] or ""),
          dict(results))
    client.media[gid] = [{"id": "m1", "status": "PROCESSING"}]
    results = MediaBackfill(client, store, log=lambda *_: None).run(products)
    check("imagem ainda processando: nao marca como feito", results["processando"] == 1
          and store.items()[("product_media", "product:1")]["status"] == "partial", dict(results))
    with store.conn:
        store.conn.execute("DELETE FROM load_items WHERE entity = 'product_media'")
    client.media[gid] = client.media[gid][:0] + [{"id": "m", "status": "READY"}]
    item = products[0]
    item.variables["input"]["files"] = item.variables["input"]["files"] * 2
    results = MediaBackfill(client, store, log=lambda *_: None).run(products)
    check("com parte das imagens: incompleto, sem reenviar (nao duplica)",
          results["incompleto"] == 1 and len(calls_of(client, "productUpdate")) == 1, dict(results))
    store.close()


def test_metaobjects_and_cleanup(tmp: Path) -> None:
    print("\nMetaobjetos, arquivos e limpeza")
    from shopify_load.cleanup import cleanup, retired

    transform_db = tmp / "shopify.sqlite"
    store = TransformStore(transform_db)
    run_id = store.start_run("2026-09-13", ["x"])
    store.replace("metaobject_definition", [Payload(
        "metaobject_definition", "metaobject_definition:selo", "metaobjectDefinitionCreate",
        {"definition": {"type": "selo", "fieldDefinitions": [{"key": "nome", "type": "single_line_text_field"}]}},
        provides=["bagy-ref:metaobject_definition:selo"])], run_id)
    store.replace("file", [Payload("file", "file:abc", "fileCreate", {"files": [
        {"originalSource": "https://cdn/selo-vegan.jpg?v=1", "contentType": "IMAGE"}]},
        provides=["bagy-ref:file:abc"])], run_id)
    store.replace("metaobject", [Payload("metaobject", "metaobject:selo:selo-vegan", "metaobjectUpsert", {
        "handle": {"type": "selo", "handle": "selo-vegan"},
        "metaobject": {"fields": [{"key": "nome", "value": "Vegano"}, {"key": "imagem", "value": "bagy-ref:file:abc"}]}},
        provides=["bagy-ref:metaobject:selo:selo-vegan"])], run_id)
    store.replace("page", [Payload("page", "hotsite:1", "pageCreate", {"page": {"title": "Hot", "handle": "hot"}},
                                   provides=["bagy-ref:page:page:1"])], run_id)
    store.close()
    snapshot = SnapshotStore(tmp / "snap.sqlite")
    for resource in ("shop", "metaobject_definitions", "metaobjects", "files", "pages"):
        snapshot.replace(resource, [], 1)
    diff.run(transform_db, snapshot)
    snapshot.close()

    load_store = LoadStore(transform_db)
    grouped = load_items(load_store.conn)
    client = FakeClient()
    results = Loader(client, load_store, log=lambda *_: None).run(plan_all(grouped))
    sent = calls_of(client, "metaobjectUpsert")
    check("metaobjeto usa o ID do arquivo criado (fileCreate devolve lista)",
          sent and sent[0]["metaobject"]["fields"][1]["value"].startswith("gid://shopify/MediaImage/")
          and results["file"]["criado"] == 1 and results["metaobject"]["criado"] == 1, (sent, dict(results)))

    with load_store.conn:
        load_store.conn.execute("UPDATE payloads SET status = 'skipped' WHERE entity = 'page'")
    check("pagina criada pela carga e nao mais planejada entra na limpeza",
          [row[:2] for row in retired(load_store)] == [("page", "hotsite:1")], retired(load_store))
    cleanup(client, load_store, log=lambda *_: None)
    row = load_store.items()[("page", "hotsite:1")]
    check("limpeza apaga na loja e esquece as refs",
          calls_of(client, "pageDelete") and row["status"] == "removed"
          and "bagy-ref:page:page:1" not in load_store.refs(), row)
    check("nada mais para limpar (metaobjeto e arquivo em uso nao entram)", retired(load_store) == [])

    with load_store.conn:
        load_store.conn.execute("UPDATE payloads SET hash = 'novo' WHERE entity IN ('metaobject', 'file')")
        load_store.conn.execute("UPDATE existing SET payload_hash = 'novo' WHERE entity IN ('metaobject', 'file')")
    grouped = load_items(load_store.conn)
    client2 = FakeClient()
    Loader(client2, load_store, log=lambda *_: None).run({"file": grouped["file"], "metaobject": grouped["metaobject"]})
    check("conteudo mudou: metaobjectUpsert reenviado; fileCreate nao (criaria duplicata)",
          len(calls_of(client2, "metaobjectUpsert")) == 1 and not calls_of(client2, "fileCreate"), client2.calls)

    from shopify_load.cleanup import plan
    definition_gid = load_store.items()[("metaobject_definition", "metaobject_definition:selo")]["shopify_id"]
    snap = SnapshotStore(tmp / "snap.sqlite")
    snap.replace("metaobject_definitions", [{"id": definition_gid, "type": "selo",
                                             "fields": [{"key": "nome", "type": "json"}]}], 2)
    planned = {(p["entity"], p["key"]): p for p in plan(load_store, snap)}
    check("definicao com campo de outro tipo na loja: recriar, e o metaobjeto some junto",
          ("metaobject_definition", "metaobject_definition:selo") in planned
          and planned.get(("metaobject", "metaobject:selo:selo-vegan"), {}).get("with_definition"), planned)
    client3 = FakeClient()
    cleanup(client3, load_store, snapshot=snap, log=lambda *_: None)
    check("apaga a definicao; metaobjeto marcado como removido sem metaobjectDelete proprio",
          calls_of(client3, "metaobjectDefinitionDelete") and not calls_of(client3, "metaobjectDelete")
          and load_store.items()[("metaobject", "metaobject:selo:selo-vegan")]["status"] == "removed"
          and "bagy-ref:metaobject:selo:selo-vegan" not in load_store.refs(), client3.calls)
    snap.close()
    load_store.close()


def test_phone_rejected(tmp: Path) -> None:
    print("\nTelefone recusado pela Shopify")
    store, grouped = setup(tmp)
    client = FakeClient()
    client.fail["customerSet"] = [{"field": ["input", "phone"], "message": "Phone is invalid"}]
    grouped["customer"][0].variables["input"]["phone"] = "+551188887777"
    results = Loader(client, store, log=lambda *_: None).run({"customer": grouped["customer"]})
    sent = calls_of(client, "customerSet")
    row = store.items()[("customer", "customer:100")]
    check("reenvia uma vez sem telefone e cria o cliente, com aviso",
          len(sent) == 2 and "phone" not in json.dumps(sent[1]) and results["customer"]["criado"] == 1
          and "telefone recusado" in (row["warnings"] or ""), (sent, dict(results), row))

    client = FakeClient()
    client.fail["orderCreate"] = [{"field": ["order", "email"], "message": "Email is invalid"}]
    from shopify_load.loader import phone_only
    check("erro que nao e de telefone nao dispara o reenvio",
          not phone_only([{"field": ["order", "email"], "message": "Email is invalid"}])
          and phone_only([{"field": ["order", "shippingAddress", "phone"], "message": "is invalid"}]))
    store.close()


def test_content_images(tmp: Path) -> None:
    print("\nImagens da Bagy no texto dos artigos")
    from shopify_load.content_images import ContentImages

    store, _ = setup(tmp)
    store.mark("article", "post:1", "done", shopify_id="gid://shopify/Article/1")
    store.mark("article", "post:2", "done", shopify_id="gid://shopify/Article/2")
    client = FakeClient()
    banner = "https://api4.dooca.store/uploads/banner-1.webp"
    client.bodies = {
        "gid://shopify/Article/1": f'<p><a href="/p"><img src="{banner}" data-image="x"></a></p><img src="{banner}">',
        "gid://shopify/Article/2": "<p>Sem imagem da Bagy</p>",
    }
    available = lambda url: (True, None)
    dry = ContentImages(client, store, dry_run=True, log=lambda *_: None, probe=available).run(("article",))
    check("dry-run le o texto e nao sobe nada", dry["enviaria"] == 1 and not calls_of(client, "fileCreate"), dict(dry))

    missing = "https://api4.dooca.store/uploads/apagada.webp"
    client.bodies["gid://shopify/Article/3"] = f'<img src="{missing}">'
    store.mark("article", "post:3", "done", shopify_id="gid://shopify/Article/3")
    gone = ContentImages(client, store, log=lambda *_: None, poll_seconds=0,
                         probe=lambda url: (url != missing, "imagem nao existe mais na Bagy (HTTP 404)")).run(("article",))
    check("imagem que nao existe mais na Bagy: nao cria arquivo e o texto fica como esta",
          gone["falhou"] == 1 and "apagada.webp" in client.bodies["gid://shopify/Article/3"]
          and "404" in ((store.asset(missing) or {}).get("error") or ""), dict(gone))
    with store.conn:
        store.conn.execute("DELETE FROM load_items WHERE entity = 'article' AND source_key = 'post:3'")
    client.calls.clear()
    for key in list(client.bodies):
        if key.endswith("/1"):
            client.bodies[key] = f'<p><a href="/p"><img src="{banner}" data-image="x"></a></p><img src="{banner}">'
    with store.conn:
        store.conn.execute("DELETE FROM load_assets WHERE source_url = ?", (banner,))

    results = ContentImages(client, store, log=lambda *_: None, poll_seconds=0, probe=available).run(("article",))
    body = client.bodies["gid://shopify/Article/1"]
    check("mesma imagem repetida sobe uma vez e as duas ocorrencias sao trocadas",
          results["corrigido"] == 1 and len(calls_of(client, "fileCreate")) == 1
          and "dooca.store" not in body and body.count("cdn.shopify.com") == 2, (dict(results), body))
    check("artigo sem imagem da Bagy nao e tocado",
          results["sem imagem"] == 1 and len(calls_of(client, "articleUpdate")) == 1)
    check("imagem enviada fica registrada", (store.asset(banner) or {}).get("status") == "ready")

    client.bodies["gid://shopify/Article/2"] = f'<img src="{banner}">'
    again = ContentImages(client, store, log=lambda *_: None, poll_seconds=0, probe=available).run(("article",))
    check("imagem ja enviada e reaproveitada em outro artigo, sem novo fileCreate",
          again["corrigido"] == 1 and len(calls_of(client, "fileCreate")) == 1, dict(again))
    store.close()


def test_invalidate_values(tmp: Path) -> None:
    print("\nValores apagados junto com a definicao")
    from shopify_load.cleanup import invalidate_values

    transform_db = tmp / "shopify.sqlite"
    store = TransformStore(transform_db)
    run_id = store.start_run("2026-09-13", ["x"])
    store.replace("product_content", [
        Payload("product_content", "product_content:1", "metafieldsSet", {"metafields": [
            {"ownerId": "gid://shopify/Product/1", "namespace": "custom", "key": "selos",
             "type": "list.file_reference", "value": "[]"}]}),
        Payload("product_content", "product_content:2", "metafieldsSet", {"metafields": [
            {"ownerId": "gid://shopify/Product/2", "namespace": "custom", "key": "modo_de_uso",
             "type": "rich_text_field", "value": "{}"}]}),
    ], run_id)
    store.close()
    load_store = LoadStore(transform_db)
    for key in ("product_content:1", "product_content:2"):
        load_store.mark("product_content", key, "done", shopify_id=None)
    count = invalidate_values(load_store, "product.custom.selos")
    items = load_store.items()
    check("so quem gravava o campo apagado volta a pendente",
          count == 1 and items[("product_content", "product_content:1")]["status"] == "removed"
          and items[("product_content", "product_content:2")]["status"] == "done", items)
    load_store.close()


def test_dependencies(tmp: Path) -> None:
    print("\nDependencias no --only")
    from shopify_load.cli import add_dependencies

    store, grouped = setup(tmp)
    plan = {"product": list(grouped["product"])}
    added = add_dependencies(plan, grouped, lambda item: True)
    check("--only product traz colecao e pagina de conteudo", added == {"collection": 1, "page": 1}
          and [i.source_key for i in plan["page"]] == ["hotsite:1"], dict(added))
    plan = {"product": list(grouped["product"])}
    added = add_dependencies(plan, grouped, lambda item: item.entity != "collection")
    check("dependencia ja carregada nao entra de novo", added == {"page": 1} and "collection" not in plan, dict(added))
    store.close()


def test_documents(tmp: Path) -> None:
    print("\nDocumentos da carga contra o schema")
    if not shutil.which("node"):
        print("  [pulado] node nao encontrado")
        return
    from shopify_load import content_images
    for name, document in (("productSet com metafield da variante", PRODUCT_SET),
                           ("productUpdate(media:)", MEDIA_ADD), ("status das imagens", MEDIA_STATUS),
                           ("leitura do texto do artigo", content_images.READ["article"]),
                           ("leitura do texto da pagina", content_images.READ["page"]),
                           ("articleUpdate(body)", content_images.UPDATE["article"][2]),
                           ("pageUpdate(body)", content_images.UPDATE["page"][2]),
                           ("fileCreate das imagens do texto", content_images.FILE_CREATE),
                           ("status da imagem do texto", content_images.FILE_STATUS)):
        path = tmp / "doc.graphql"
        path.write_text(document, encoding="utf-8")
        result = subprocess.run(["node", str(ROOT / "tools" / "validate_gql.mjs"), str(path)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = (result.stdout + result.stderr).strip()
        check(f"{name} valido", result.returncode == 0 and "INVALID" not in output, output[-500:])


def main() -> int:
    test_resolve()
    for test in (test_dry_run, test_pilot, test_load, test_resume_and_uncertain, test_media, test_dependencies,
                 test_metaobjects_and_cleanup, test_invalidate_values, test_phone_rejected, test_content_images,
                 test_documents):
        with tempfile.TemporaryDirectory() as tmp:
            test(Path(tmp))
    print(f"\n{'TUDO OK' if FAILURES == 0 else f'{FAILURES} FALHA(S)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
