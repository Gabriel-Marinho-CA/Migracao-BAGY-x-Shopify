"""Testes do snapshot e do diff com a loja Shopify, sem rede (fixtures sinteticas).

Tambem valida todas as consultas do snapshot contra o schema da Admin API.

Uso: python tools/test_shopify_diff.py
"""

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bagy2shopify.shopify_client import ShopifyClient
from bagy_transform.model import Payload
from bagy_transform.store import TransformStore
from shopify_snapshot import diff, queries
from shopify_snapshot.storage import SnapshotStore

FAILURES = 0


def check(description: str, condition, detail=None) -> None:
    global FAILURES
    print(f"  [{'ok ' if condition else 'FALHOU'}] {description}")
    if not condition:
        FAILURES += 1
        if detail is not None:
            print(f"         {detail}")


def gid(kind: str, number) -> str:
    return f"gid://shopify/{kind}/{number}"


# --------------------------------------------------------------------------- #
# Fixtures: payloads

def product(product_id, handle, sku, title, variation_id):
    return Payload("product", f"product:{product_id}", "productSet", variables={
        "identifier": {"handle": handle},
        "input": {"title": title, "handle": handle,
                  "variants": [{"sku": sku, "metafields": [
                      {"namespace": "bagy", "key": "variation_id", "type": "number_integer",
                       "value": str(variation_id)}]}],
                  "metafields": [{"namespace": "bagy", "key": "product_id", "type": "number_integer",
                                  "value": str(product_id)}]}},
        provides=[f"bagy-ref:product:{product_id}", f"bagy-ref:variant:{variation_id}"])


def customer(customer_id, email, phone=None):
    return Payload("customer", f"customer:{customer_id}", "customerSet", variables={
        "identifier": {"email": email}, "input": {"email": email, "phone": phone}},
        post_actions=[{"mutation": "metafieldsSet", "variables": {"metafields": [
            {"namespace": "bagy", "key": "customer_id", "type": "number_integer", "value": str(customer_id),
             "ownerId": "bagy-ref:self"}]}}],
        provides=[f"bagy-ref:customer:{customer_id}"])


def order(order_id, code, processed_at, total):
    return Payload("order", f"order:{order_id}", "orderCreate", variables={"order": {
        "name": f"#{code}", "sourceIdentifier": f"bagy-{order_id}", "processedAt": processed_at,
        "tags": ["bagy-import", f"bagy-id-{order_id}"]}},
        provides=[f"bagy-ref:order:{order_id}"], meta={"total_shopify": total, "total_bagy": total})


PAYLOADS = {
    "metafield_definition": [
        Payload("metafield_definition", "def:a", "metafieldDefinitionCreate", variables={"definition": {
            "ownerType": "PRODUCT", "namespace": "custom", "key": "a", "type": "single_line_text_field"}},
            provides=["bagy-ref:metafield_definition:a"]),
        Payload("metafield_definition", "def:b", "metafieldDefinitionCreate", variables={"definition": {
            "ownerType": "PRODUCT", "namespace": "custom", "key": "b", "type": "list.single_line_text_field"}},
            provides=["bagy-ref:metafield_definition:b"]),
    ],
    "collection": [
        Payload("collection", "category:5", "collectionCreate", variables={"collection": {
            "title": "Kits", "handle": "kits",
            "metafields": [{"namespace": "bagy", "key": "category_id", "type": "number_integer", "value": "5"}]}},
            provides=["bagy-ref:collection:category:5"]),
    ],
    "product": [
        product(1, "a", "SKU-A", "Produto A", 11),       # casa pelo metafield
        product(2, "b", "SKU-B", "Produto B", 22),       # handle tomado por outro; casa por sku
        product(3, "c", "SKU-C", "Produto C", 33),       # so pelo titulo
        product(4, "d", "SKU-D", "Produto D", 44),       # nao existe
        product(5, "e", "SKU-E", "Produto E", 55),       # sku em dois produtos: conflito
        product(6, "f", "SKU-F", "Produto F", 66),       # handle de outro produto da Bagy, sem outro casamento
    ],
    "customer": [customer(100, "Cli@Teste.com", "+5511988887777"), customer(101, "novo@teste.com")],
    "lead": [Payload("lead", "lead:abc", "customerSet", variables={
        "identifier": {"email": "lead@teste.com"}, "input": {"email": "lead@teste.com"}},
        provides=["bagy-ref:lead:abc"])],
    "store_credit": [
        Payload("store_credit", "cashback:1", "storeCreditAccountCredit", variables={
            "id": "bagy-ref:customer:100", "creditInput": {"creditAmount": {"amount": "10.00", "currencyCode": "BRL"}}}),
        Payload("store_credit", "cashback:2", "storeCreditAccountCredit", variables={
            "id": "bagy-ref:customer:101", "creditInput": {"creditAmount": {"amount": "5.00", "currencyCode": "BRL"}}}),
    ],
    "discount": [
        Payload("discount", "coupon:1", "discountCodeBasicCreate",
                variables={"basicCodeDiscount": {"title": "News", "code": "NEWS10"}}, provides=["bagy-ref:discount:1"]),
        Payload("discount", "coupon:2", "discountCodeBasicCreate",
                variables={"basicCodeDiscount": {"title": "Novo", "code": "NOVO"}}, provides=["bagy-ref:discount:2"]),
    ],
    "blog": [Payload("blog", "blog", "blogCreate", variables={"blog": {"title": "Blog", "handle": "blog"}},
                     provides=["bagy-ref:blog:main"])],
    "article": [
        Payload("article", "post:1", "articleCreate", variables={"article": {
            "blogId": "bagy-ref:blog:main", "title": "Post Um", "handle": "post-um"}}, provides=["bagy-ref:article:1"]),
        Payload("article", "post:2", "articleCreate", variables={"article": {
            "blogId": "bagy-ref:blog:main", "title": "Post Dois", "handle": "post-dois"}}, provides=["bagy-ref:article:2"]),
    ],
    "policy": [
        Payload("policy", "policy:refund", "shopPolicyUpdate",
                variables={"shopPolicy": {"type": "REFUND_POLICY", "body": "<p>Trocas  em 7 dias</p>"}}),
        Payload("policy", "policy:privacy", "shopPolicyUpdate",
                variables={"shopPolicy": {"type": "PRIVACY_POLICY", "body": "<p>Nova</p>"}}),
        Payload("policy", "policy:shipping", "shopPolicyUpdate",
                variables={"shopPolicy": {"type": "SHIPPING_POLICY", "body": "<p>Frete</p>"}}),
    ],
    "page": [Payload("page", "page:sobre", "pageCreate", variables={"page": {"title": "Sobre", "handle": "sobre"}},
                     provides=["bagy-ref:page:page:1"])],
    "redirect": [
        Payload("redirect", "redirect:a", "urlRedirectCreate",
                variables={"urlRedirect": {"path": "/a", "target": "/products/a"}}),
        Payload("redirect", "redirect:b", "urlRedirectCreate",
                variables={"urlRedirect": {"path": "/b", "target": "/products/b"}}),
        Payload("redirect", "redirect:c", "urlRedirectCreate",
                variables={"urlRedirect": {"path": "/c", "target": "/"}}),
    ],
    "order": [
        order(1, "17309030526531", "2025-01-10T10:05:12-03:00", "10.00"),   # sourceIdentifier
        order(2, "17309030526532", "2025-02-10T10:05:12-03:00", "20.00"),   # codigo na nota
        order(3, "17309030526533", "2025-03-10T10:05:12-03:00", "60.31"),   # data + total
        order(4, "17309030526534", "2025-04-10T10:05:12-03:00", "99.00"),   # sem par
    ],
}

# --------------------------------------------------------------------------- #
# Fixtures: snapshot (ja normalizado, como sai de queries.norm_*)

SNAPSHOT = {
    "shop": [{"id": gid("Shop", 1), "domain": "loja.myshopify.com", "created_at": "2020-01-01T00:00:00Z"}],
    "policies": [
        {"id": gid("ShopPolicy", 1), "type": "REFUND_POLICY", "body": "<div>Trocas em 7 dias</div>"},
        {"id": gid("ShopPolicy", 2), "type": "PRIVACY_POLICY", "body": "<p>Antiga</p>"},
    ],
    "locations": [{"id": gid("Location", 9), "is_active": True, "is_primary": True}],
    "publications": [{"id": gid("Publication", 7), "name": "Online Store"}],
    "metafield_definitions": [
        {"id": gid("MetafieldDefinition", 1), "owner_type": "PRODUCT", "namespace": "custom", "key": "a",
         "type": "single_line_text_field"},
        {"id": gid("MetafieldDefinition", 2), "owner_type": "PRODUCT", "namespace": "custom", "key": "b",
         "type": "single_line_text_field"},
    ],
    "collections": [{"id": gid("Collection", 1), "handle": "kits", "title": "Kits", "category_id": None}],
    "products": [
        {"id": gid("Product", 1), "handle": "a", "title": "Produto A", "product_id": "1",
         "variants": [{"id": gid("ProductVariant", 1), "sku": "SKU-A", "variation_id": "11"}]},
        {"id": gid("Product", 2), "handle": "b-importado", "title": "Outro nome", "product_id": None,
         "variants": [{"id": gid("ProductVariant", 2), "sku": "sku-b", "variation_id": None}]},
        {"id": gid("Product", 3), "handle": "b", "title": "Produto de outro id", "product_id": "999",
         "variants": [{"id": gid("ProductVariant", 3), "sku": "X", "variation_id": None}]},
        {"id": gid("Product", 4), "handle": "produto-c-1", "title": "Produto  C", "product_id": None,
         "variants": [{"id": gid("ProductVariant", 4), "sku": None, "variation_id": None}]},
        {"id": gid("Product", 5), "handle": "e1", "title": "E1", "product_id": None,
         "variants": [{"id": gid("ProductVariant", 5), "sku": "SKU-E", "variation_id": None}]},
        {"id": gid("Product", 6), "handle": "e2", "title": "E2", "product_id": None,
         "variants": [{"id": gid("ProductVariant", 6), "sku": "SKU-E", "variation_id": None}]},
        {"id": gid("Product", 7), "handle": "f", "title": "Outro F", "product_id": "888",
         "variants": [{"id": gid("ProductVariant", 7), "sku": "Y", "variation_id": None}]},
        {"id": gid("Product", 8), "handle": "a-1", "title": "Produto A", "product_id": None,
         "variants": [{"id": gid("ProductVariant", 8), "sku": "Z", "variation_id": None}]},
        {"id": gid("Product", 9), "handle": "sobra", "title": "Sobra", "product_id": None, "variants": []},
    ],
    "customers": [
        {"id": gid("Customer", 1), "email": "cli@teste.com", "phone": None, "customer_id": None,
         "store_credit": [{"id": gid("StoreCreditAccount", 1), "balance": "10.0", "currency": "BRL"}]},
        {"id": gid("Customer", 2), "email": "outro@teste.com", "phone": "+5511988887777", "customer_id": None,
         "store_credit": []},
    ],
    "discounts": [{"id": gid("DiscountCodeNode", 1), "type": "DiscountCodeBasic", "codes": ["news10"],
                   "codes_count": 1}],
    "blogs": [{"id": gid("Blog", 1), "handle": "news", "title": "Blog"}],
    "articles": [
        {"id": gid("Article", 1), "handle": "post-um", "title": "Post Um", "blog_id": gid("Blog", 1)},
        {"id": gid("Article", 2), "handle": "post-dois", "title": "Post Dois", "blog_id": gid("Blog", 5)},
    ],
    "pages": [],
    "menus": [],
    "url_redirects": [
        {"id": gid("UrlRedirect", 1), "path": "/A/", "target": "/products/a"},
        {"id": gid("UrlRedirect", 2), "path": "/b", "target": "/collections/all"},
    ],
    "orders": [
        {"id": gid("Order", 1), "name": "#1001", "source_identifier": "bagy-1", "tags": [], "tokens": [],
         "processed_at": "2025-01-10T13:05:12Z", "total": "10.00", "app": "Bagy2Shopify"},
        {"id": gid("Order", 2), "name": "#1002", "source_identifier": None, "tags": ["importado"],
         "tokens": ["17309030526532"], "processed_at": "2026-01-01T00:00:00Z", "total": "1.00", "app": "Matrixify"},
        {"id": gid("Order", 3), "name": "#1003", "source_identifier": None, "tags": [], "tokens": [],
         "processed_at": "2025-03-10T13:05:40Z", "total": "60.31", "app": "Matrixify"},
        {"id": gid("Order", 4), "name": "#1004", "source_identifier": None, "tags": [], "tokens": [],
         "processed_at": "2024-01-01T00:00:00Z", "total": "5.00", "app": "Matrixify"},
    ],
}


def build(tmp: Path, scopes: list, forbidden=("pages",)) -> tuple:
    transform_db = tmp / "shopify.sqlite"
    store = TransformStore(transform_db)
    run_id = store.start_run("2026-09-12 00:00", list(PAYLOADS))
    for entity, payloads in PAYLOADS.items():
        store.replace(entity, payloads, run_id)
    store.close()

    snapshot = SnapshotStore(tmp / "snapshot.sqlite")
    for resource, records in SNAPSHOT.items():
        snapshot.replace(resource, records, 1)
    for resource in forbidden:
        snapshot.fail(resource, "forbidden", "Access denied for pages field")
    snapshot.set_meta("access_scopes", scopes)
    snapshot.set_meta("shop", SNAPSHOT["shop"][0])
    return transform_db, snapshot


def load_existing(transform_db: Path) -> tuple:
    conn = sqlite3.connect(str(transform_db))
    rows = {(entity, key): {"action": action, "id": shopify_id, "by": match_by, "weak": bool(weak),
                            "notes": json.loads(notes), "candidates": json.loads(candidates)}
            for entity, key, action, shopify_id, match_by, weak, notes, candidates in conn.execute(
                "SELECT entity, source_key, action, shopify_id, match_by, weak_match, notes, candidates FROM existing")}
    refs = dict(conn.execute("SELECT ref, shopify_id FROM ref_map"))
    conn.close()
    return rows, refs


def test_diff(tmp: Path) -> None:
    print("\nDiff (sem read_all_orders, pages sem permissao)")
    transform_db, snapshot = build(tmp, ["read_products", "write_customers"])
    report = diff.run(transform_db, snapshot, currency="BRL", match_by_title=True, out_dir=tmp / "out")
    snapshot.close()
    rows, refs = load_existing(transform_db)

    def row(entity, key):
        return rows.get((entity, key)) or {}

    check("definicao igual: ja existe", row("metafield_definition", "def:a")["action"] == "skip")
    check("definicao com outro tipo: conflito", row("metafield_definition", "def:b")["action"] == "conflict")
    check("colecao pelo handle", row("collection", "category:5")["action"] == "skip"
          and row("collection", "category:5")["by"] == "handle")

    a = row("product", "product:1")
    check("produto pelo metafield bagy.product_id -> atualizar", a["action"] == "update" and a["by"] == "bagy.product_id", a)
    check("produto A: variante mapeada pela variation_id", refs.get("bagy-ref:variant:11") == gid("ProductVariant", 1), refs)
    check("produto A: duplicata pelo titulo apontada", gid("Product", 8) in a["candidates"]
          and any("duplicata" in n for n in a["notes"]), a)
    b = row("product", "product:2")
    check("produto B: handle de outro produto da Bagy nao casa; casa por sku", b["action"] == "update"
          and b["id"] == gid("Product", 2) and b["by"] == "sku", b)
    check("produto B: avisa handle diferente e handle ocupado",
          any("identifier.id" in n for n in b["notes"]) and any("handle ja usado" in n for n in b["notes"]), b)
    check("produto B: variante unica mapeada pelo sku", refs.get("bagy-ref:variant:22") == gid("ProductVariant", 2))
    c = row("product", "product:3")
    check("produto C: so pelo titulo, marcado como fraco", c["action"] == "update" and c["weak"] and c["by"] == "titulo", c)
    check("produto D: criar", row("product", "product:4")["action"] == "create")
    e = row("product", "product:5")
    check("produto E: sku em dois produtos -> conflito", e["action"] == "conflict" and len(e["candidates"]) == 2, e)
    f = row("product", "product:6")
    check("produto F: handle ocupado por outro id da Bagy -> conflito", f["action"] == "conflict"
          and f["candidates"] == [gid("Product", 7)], f)

    x = row("customer", "customer:100")
    check("cliente pelo e-mail sem diferenciar maiusculas -> atualizar", x["action"] == "update" and x["by"] == "email", x)
    check("cliente: telefone ja usado por outro cliente", any("telefone" in n for n in x["notes"]), x)
    check("cliente novo e lead novo: criar", row("customer", "customer:101")["action"] == "create"
          and row("lead", "lead:abc")["action"] == "create")
    check("credito com o mesmo saldo: ja existe", row("store_credit", "cashback:1")["action"] == "skip")
    check("credito de cliente novo: criar", row("store_credit", "cashback:2")["action"] == "create")

    check("desconto pelo codigo sem diferenciar maiusculas", row("discount", "coupon:1")["action"] == "skip")
    check("desconto novo: criar", row("discount", "coupon:2")["action"] == "create")
    check("blog so pelo titulo (handle diferente)", row("blog", "blog")["action"] == "skip" and row("blog", "blog")["weak"])
    check("artigo no blog casado: pelo blog+handle", row("article", "post:1")["by"] == "blog+handle")
    check("artigo com o handle em outro blog: fraco", row("article", "post:2")["by"] == "handle em outro blog"
          and row("article", "post:2")["weak"], row("article", "post:2"))
    check("politica com o mesmo texto (HTML diferente): ja existe", row("policy", "policy:refund")["action"] == "skip")
    check("politica com outro texto: atualizar", row("policy", "policy:privacy")["action"] == "update")
    check("politica inexistente: criar", row("policy", "policy:shipping")["action"] == "create")
    check("pagina sem permissao de leitura: nao conferido", row("page", "page:sobre")["action"] == "unverified")
    check("redirect com caminho igual (maiuscula e barra) e mesmo destino", row("redirect", "redirect:a")["action"] == "skip")
    check("redirect com outro destino: conflito", row("redirect", "redirect:b")["action"] == "conflict")
    check("redirect novo: criar", row("redirect", "redirect:c")["action"] == "create")

    check("pedido pelo sourceIdentifier", row("order", "order:1")["by"] == "sourceIdentifier")
    check("pedido pelo codigo gravado em outro campo", row("order", "order:2")["by"] == "codigo do pedido")
    check("pedido pela data (fuso diferente) + total", row("order", "order:3")["by"] == "data+total"
          and row("order", "order:3")["weak"], row("order", "order:3"))
    check("pedido sem par e sem read_all_orders: nao conferido", row("order", "order:4")["action"] == "unverified")

    check("ref_map: local e canal padrao", refs.get("bagy-ref:location:default") == gid("Location", 9)
          and refs.get("bagy-ref:publication:online_store") == gid("Publication", 7), refs)
    check("ref_map: nada de conflito nem de criar", "bagy-ref:product:5" not in refs and "bagy-ref:product:4" not in refs)
    orphan_products = report["orphans"]["products"]
    check("orfaos de produto contados", orphan_products["sem_par"] == 2 and "sobra" in orphan_products["exemplos"],
          orphan_products)
    check("orfaos de pedido por app", report["orphans"]["orders"]["app"] == {"Matrixify": 1}, report["orphans"]["orders"])
    check("arquivos de revisao gerados", (tmp / "out" / "_diff.json").exists()
          and (tmp / "out" / "diff_revisar.jsonl").exists())


def test_diff_all_orders(tmp: Path) -> None:
    print("\nDiff com read_all_orders")
    transform_db, snapshot = build(tmp, ["read_products", "read_all_orders"], forbidden=())
    diff.run(transform_db, snapshot, currency="BRL", match_by_title=False)
    snapshot.close()
    rows, _ = load_existing(transform_db)
    check("pedido sem par: criar", rows[("order", "order:4")]["action"] == "create")
    check("pagina lida: criar", rows[("page", "page:sobre")]["action"] == "create")
    check("sem casar por titulo: produto C vira criar", rows[("product", "product:3")]["action"] == "create")


def test_jsonl_and_normalize() -> None:
    print("\nJSONL da bulk operation e normalizacao")
    lines = [
        json.dumps({"id": gid("Product", 1), "handle": "a", "title": "A", "status": "ACTIVE",
                    "metafield": {"value": "7"}}).encode(),
        b"",
        json.dumps({"id": gid("ProductVariant", 9), "sku": "S", "barcode": None, "metafield": None,
                    "__parentId": gid("Product", 1)}).encode(),
    ]
    records = queries.parse_jsonl(lines, queries.norm_product)
    check("variante volta para dentro do produto", len(records) == 1
          and records[0]["variants"][0]["id"] == gid("ProductVariant", 9) and records[0]["product_id"] == "7", records)
    customer_lines = [
        json.dumps({"id": gid("Customer", 1), "defaultEmailAddress": {"emailAddress": "A@B.com"}}),
        json.dumps({"id": gid("StoreCreditAccount", 3), "balance": {"amount": "4.5", "currencyCode": "BRL"},
                    "__parentId": gid("Customer", 1)}),
    ]
    customers = queries.parse_jsonl(customer_lines, queries.norm_customer)
    check("e-mail em minusculas e saldo de credito", customers[0]["email"] == "a@b.com"
          and customers[0]["store_credit"][0]["balance"] == "4.5", customers)
    record = queries.norm_order({"id": gid("Order", 1), "name": "#1001", "note": "Pedido Bagy 17309030526534",
                                 "customAttributes": [{"key": "CPF", "value": "123.456.789-09"}], "tags": []})
    check("pedido: guarda o codigo longo, nao a nota nem o CPF formatado",
          record["tokens"] == ["17309030526534"] and "note" not in record, record)
    check("select sempre inclui a loja", [r.name for r in queries.select(["pedidos"])] == ["shop", "orders"])


def test_files_diff() -> None:
    print("\nArquivos: casa pelo nome sem extensao")
    from bagy_transform.model import Payload as TransformPayload
    item = diff.Item("file", "file:abc", "fileCreate",
                     {"files": [{"originalSource": "https://cdn.dooca.store/1/files/selo-vegan.jpg?v=1"}]},
                     [], ["bagy-ref:file:abc"], "h", {})
    other = diff.Item("file", "file:def", "fileCreate",
                      {"files": [{"originalSource": "https://cdn.dooca.store/1/files/novo.png"}]},
                      [], ["bagy-ref:file:def"], "h", {})
    rows, _ = diff.diff_files([item, other], [{"id": gid("MediaImage", 1), "filename": "selo-vegan.webp"}])
    check("selo-vegan.jpg na origem = selo-vegan.webp na loja (convertido pelo CDN)",
          rows["file:abc"].action == "skip" and rows["file:abc"].shopify_id == gid("MediaImage", 1), rows["file:abc"])
    check("arquivo que nao existe na loja: criar", rows["file:def"].action == "create")


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.headers = {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_token_refresh() -> None:
    print("\nToken renovado no HTTP 401")
    state = {"token": "velho", "calls": 0}

    def provider(force=False):
        state["calls"] += 1
        if force:
            state["token"] = "novo"
        return state["token"]

    client = ShopifyClient("loja.myshopify.com", "", "2026-07", orders_per_minute=0, max_retries=1,
                           token_provider=provider)
    responses = [FakeResponse(401), FakeResponse(200, {"data": {"shop": {"id": "1"}},
                                                       "extensions": {"cost": {"requestedQueryCost": 1}}})]
    sent = []

    def post(url, data=None, timeout=None):
        sent.append(client.session.headers["X-Shopify-Access-Token"])
        return responses.pop(0)

    client.session.post = post
    data = client.execute("{ shop { id } }")
    check("repete a chamada com o token novo", data == {"shop": {"id": "1"}} and sent == ["velho", "novo"], sent)
    check("guarda o custo da query", client.last_extensions.get("cost", {}).get("requestedQueryCost") == 1)


def test_graphql_documents(tmp: Path) -> None:
    print("\nConsultas do snapshot contra o schema (validate_gql.mjs)")
    if not shutil.which("node"):
        print("  [pulado] node nao encontrado")
        return
    documents = {name: resource.query for name, resource in queries.RESOURCES.items()}
    documents["bulk_run"] = queries.BULK_RUN
    documents["bulk_status"] = queries.BULK_STATUS
    for name, document in documents.items():
        path = tmp / f"{name}.graphql"
        path.write_text(document, encoding="utf-8")
        result = subprocess.run(["node", str(ROOT / "tools" / "validate_gql.mjs"), str(path)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = (result.stdout + result.stderr).strip()
        check(f"{name} valido", result.returncode == 0 and "INVALID" not in output, output[-600:])


def test_oauth(tmp: Path) -> None:
    print("\nAutorizacao pelo navegador e token salvo (sem rede)")
    import hashlib
    import hmac as hmac_lib

    from bagy2shopify import auth, oauth

    params = [("code", "abc"), ("shop", "loja.myshopify.com"), ("state", "xyz"), ("timestamp", "1700000000")]
    message = "code=abc&shop=loja.myshopify.com&state=xyz&timestamp=1700000000"
    signature = hmac_lib.new(b"segredo", message.encode(), hashlib.sha256).hexdigest()
    check("HMAC do retorno confere", oauth.verify_hmac(params + [("hmac", signature)], "segredo"))
    check("HMAC com parametro adulterado e recusado",
          not oauth.verify_hmac([("code", "outro")] + params[1:] + [("hmac", signature)], "segredo"))
    check("retorno sem HMAC e recusado", not oauth.verify_hmac(params, "segredo"))

    path = tmp / "tokens.json"
    tokens = auth.TokenFile(path)
    tokens.save("loja.myshopify.com", auth.entry_from_response({
        "access_token": "a1", "expires_in": 10, "refresh_token": "r1",
        "refresh_token_expires_in": 7776000, "scope": "read_orders"}))
    sent = []
    original = auth.requests.post

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        sent.append(data)
        return FakeResponse(200, {"access_token": "a2", "expires_in": 3600, "refresh_token": "r2",
                                  "refresh_token_expires_in": 7776000})

    auth.requests.post = fake_post
    try:
        token = auth.OAuthToken("loja.myshopify.com", "id", "segredo", tokens)()
    finally:
        auth.requests.post = original
    check("token perto de expirar e renovado pelo refresh token",
          token == "a2" and sent and sent[0].get("grant_type") == "refresh_token"
          and sent[0].get("refresh_token") == "r1", sent)
    saved = tokens.get("loja.myshopify.com")
    check("token renovado fica salvo com o refresh token novo", saved["access_token"] == "a2"
          and saved["refresh_token"] == "r2" and saved["scope"] == "read_orders", saved)

    provider, _, _ = auth.token_provider("loja.myshopify.com", client_id="id", client_secret="s", token_file=path)
    check("token salvo tem prioridade sobre client credentials", isinstance(provider, auth.OAuthToken))
    provider, _, _ = auth.token_provider("outra.myshopify.com", client_id="id", client_secret="s", token_file=path)
    check("loja sem token salvo usa client credentials", isinstance(provider, auth.ClientCredentialsToken))


def main() -> int:
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second, \
            tempfile.TemporaryDirectory() as third, tempfile.TemporaryDirectory() as fourth:
        test_diff(Path(first))
        test_diff_all_orders(Path(second))
        test_jsonl_and_normalize()
        test_files_diff()
        test_token_refresh()
        test_oauth(Path(fourth))
        test_graphql_documents(Path(third))
    print(f"\n{'TUDO OK' if FAILURES == 0 else f'{FAILURES} FALHA(S)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
