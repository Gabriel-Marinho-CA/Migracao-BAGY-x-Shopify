"""Testes da transformacao, sem rede e sem os dados reais (fixtures sinteticas).

Cada payload gerado nos testes tambem passa pelo validador de schema da Shopify.

Uso: python tools/test_transform.py
"""

import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy_transform.config import TransformSettings, _schema_path
from bagy_transform.entities import catalog, content, customers, marketing, orders
from bagy_transform.schema import SchemaValidator
from bagy_transform.transform import validate

FAILURES = 0

SETTINGS = TransformSettings(
    source_db=Path("nao-usado"), out_db=Path("nao-usado"), out_dir=Path("nao-usado"),
    schema_path=_schema_path("2026-07"), api_version="2026-07", currency="BRL",
    timezone_offset="-03:00", fulfill_invoiced_after_days=30, order_name_from_code=True,
    include_leads=True, include_inactive_discounts=False, blog_title="Blog",
    blog_handle="blog", article_author="Loja Teste",
)
REFERENCE = datetime(2025, 6, 1, 12, 0, 0)
VALIDATOR = SchemaValidator(SETTINGS.schema_path)


def check(description: str, condition, detail=None) -> None:
    global FAILURES
    print(f"  [{'ok ' if condition else 'FALHOU'}] {description}")
    if not condition:
        FAILURES += 1
        if detail:
            print(f"         {detail}")


def schema_errors(payload) -> list:
    validate(VALIDATOR, payload)
    return payload.schema_errors


class FakeSource:
    def __init__(self, data: dict):
        self.data = data
        self.settings = {"id": 159387, "domain": "www.loja.com", "dooca_domain": "loja.bagypro.com"}
        self.shop_id = 159387
        self.store_hosts = {"www.loja.com", "loja.com", "loja.bagypro.com", "www.loja.bagypro.com"}

    def all(self, resource):
        return self.data.get(resource, [])

    def one(self, resource):
        rows = self.all(resource)
        return rows[0] if rows else None

    def extracted_at(self):
        return None

    def extraction_states(self):
        return {}


def make_order(**changes) -> dict:
    order = {
        "id": 1, "code": 17309030526534, "status": "open", "payment_status": "approved",
        "fulfillment_status": "shipped", "created_at": "2025-01-10 10:00:00",
        "updated_at": "2025-01-12 10:00:00", "canceled_at": None,
        # Caso real de arredondamento: Bagy grava 2.50 de desconto, mas o total usa 2.49.
        "subtotal": "49.90", "discount": "2.50", "tax": "0.00", "total": "60.31",
        "customer_id": 10, "device": "mobile",
        "customer": {"id": 10, "email": "Cliente@Teste.com", "cgc": "00474780040",
                     "name": "Cliente Teste", "phone": "11988887777", "entity": "individual"},
        "address": {"receiver": "Cliente Teste", "street": "Rua X", "number": "10", "detail": None,
                    "district": "Centro", "city": "Sao Paulo", "state": "SP", "zipcode": "01000000"},
        "items": [{"id": 5, "name": "Produto", "price": "49.90", "quantity": 1, "sku": "SKU1",
                   "variation_id": 100, "weight": "0.100", "is_gift": False, "is_virtual": False}],
        "shipping": {"alias": "Loggi", "name": "Loggi", "api": "frenet", "price": "12.90", "carrier_id": 1},
        "payment": {"method": "pix", "gateway_name": "Pagar.me 2.0", "discount": "2.50",
                    "processed_at": "2025-01-10 10:05:00", "updated_at": "2025-01-10 10:05:00",
                    "parcels": 1, "extra": {}},
        "fulfillment": {"shipping_code": "LGI123", "shipping_carrier": "Frenet - Loggi - Loggi",
                        "nfe_number": "1"},
        "discounts": [], "extra": {"utm": None},
        "histories": [{"status": "approved", "created_at": "2025-01-10 10:05:00", "note": None,
                       "user_type": "app"}],
    }
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(order.get(key), dict):
            order[key] = {**order[key], **value}
        else:
            order[key] = value
    return order


def test_orders() -> None:
    print("Pedidos\n")

    def run(order):
        return orders.transform_order(order, SETTINGS, REFERENCE, {100}, {10})

    payload = run(make_order())
    order = payload.variables["order"]
    code = order["discountCode"]["itemFixedDiscountCode"]
    check("desconto derivado do total: 49.90 + 12.90 - 60.31 = 2.49",
          code["amountSet"]["shopMoney"]["amount"] == "2.49")
    check("codigo do desconto identifica o Pix", code["code"] == "PIX")
    check("total enviado bate com o da Bagy",
          payload.meta["total_bagy"] == payload.meta["total_shopify"] == "60.31")
    check("aviso de arredondamento registrado", any("arredondamento" in w for w in payload.warnings))
    check("numero do pedido = codigo da Bagy", order["name"] == "#17309030526534")
    check("item ligado a variante do catalogo", order["lineItems"][0]["variantId"] == "bagy-ref:variant:100")
    check("cliente associado por referencia",
          order["customer"] == {"toAssociate": {"id": "bagy-ref:customer:10"}})
    check("pago: transacao SALE com o total",
          order["financialStatus"] == "PAID"
          and order["transactions"][0]["amountSet"]["shopMoney"]["amount"] == "60.31")
    check("enviado com rastreio e transportadora normalizada",
          order["fulfillment"]["trackingNumber"] == "LGI123"
          and order["fulfillment"]["trackingCompany"] == "Loggi")
    check("horario local convertido para ISO com -03:00",
          order["processedAt"] == "2025-01-10T10:00:00-03:00")
    check("CPF valido gera orderUpdate com TAX_CREDENTIAL_BR",
          any(a.get("mutation") == "orderUpdate" for a in payload.post_actions))
    check("pedido enviado agenda varredura de fulfillment",
          any(a.get("action") == "fulfillment_sweep" for a in payload.post_actions))
    errors = schema_errors(payload)
    check("orderCreate e pos-acoes validos no schema", not errors, errors[:3])

    payload = run(make_order(status="canceled", payment_status="denied", fulfillment_status="unfulfilled",
                             canceled_at="2025-01-11 09:00:00"))
    order = payload.variables["order"]
    cancel = [a for a in payload.post_actions if a.get("mutation") == "orderCancel"]
    check("pagamento negado: VOIDED, sem transacao e sem envio",
          order["financialStatus"] == "VOIDED" and "transactions" not in order and "fulfillment" not in order)
    check("cancelado agenda orderCancel com motivo DECLINED",
          bool(cancel) and cancel[0]["variables"]["reason"] == "DECLINED")
    errors = schema_errors(payload)
    check("pedido cancelado valido no schema", not errors, errors[:3])

    payload = run(make_order(status="canceled", payment_status="refunded", fulfillment_status="invoiced"))
    order = payload.variables["order"]
    check("estornado: SALE + REFUND e status REFUNDED",
          [t["kind"] for t in order["transactions"]] == ["SALE", "REFUND"]
          and order["financialStatus"] == "REFUNDED" and "fulfillment" not in order)

    payload = run(make_order(status="canceled", payment_status="expired", fulfillment_status="unfulfilled"))
    check("expirado: EXPIRED", payload.variables["order"]["financialStatus"] == "EXPIRED")

    old = run(make_order(fulfillment_status="invoiced", fulfillment={"shipping_code": None}))
    recent = run(make_order(fulfillment_status="invoiced", created_at="2025-05-25 10:00:00",
                            fulfillment={"shipping_code": None}))
    check("faturado ha mais de 30 dias vira enviado, sem rastreio",
          "fulfillment" in old.variables["order"]
          and "trackingNumber" not in old.variables["order"]["fulfillment"])
    check("faturado recente continua pendente de envio", "fulfillment" not in recent.variables["order"])

    payload = run(make_order(customer={"cgc": "12345678900"}))
    attributes = {a["key"]: a["value"] for a in payload.variables["order"]["customAttributes"]}
    check("CPF invalido: sem orderUpdate e com aviso",
          not any(a.get("mutation") == "orderUpdate" for a in payload.post_actions)
          and any("CPF/CNPJ invalido" in w for w in payload.warnings))
    check("CPF invalido fica nas informacoes adicionais, marcado",
          attributes.get("info_document") == "12345678900" and attributes.get("info_document_valid") == "false")

    payload = run(make_order(discounts=[{"type": "coupon", "code": "vip20", "amount": "5.00"}],
                             total="55.31"))
    check("cupom + Pix somados num codigo so",
          payload.variables["order"]["discountCode"]["itemFixedDiscountCode"]["code"] == "VIP20+PIX")


def test_customers() -> None:
    print("\nClientes\n")
    data = {
        "customers": [
            {"id": 1, "email": "A@Teste.com", "first_name": "Ana", "last_name": "Lima", "name": "Ana Lima",
             "phone": "11999998888", "cgc": "00474780040", "entity": "individual", "birthday": "1990-05-10",
             "gender": "female", "newsletter": True, "created_at": "2024-01-01 10:00:00"},
            {"id": 2, "email": "b@teste.com", "first_name": "Bruno", "last_name": "Reis", "name": "Bruno Reis",
             "phone": "(11) 99999-8888", "cgc": "11222333000181", "entity": "company", "company": "Empresa X",
             "ie": "123", "birthday": "0000-00-00", "gender": None, "newsletter": False,
             "created_at": "2024-02-01 10:00:00"},
        ],
        "customer_addresses": [
            {"customer_id": 1, "receiver": "Ana Lima", "street": "Rua A", "number": "1", "detail": "apto 2",
             "district": "Centro", "city": "Sao Paulo", "state": "SP", "zipcode": "01001000"},
        ],
        "mailings_all": [
            {"email": "lead@teste.com", "name": "Lead Teste", "phone": "11999998888", "is_customer": False,
             "created_at": "2024-03-01 10:00:00"},
            {"email": "a@teste.com", "name": "Ana", "is_customer": True, "created_at": "2024-01-01 10:00:00"},
        ],
    }
    src = FakeSource(data)
    payloads, used_phones = customers.customers(src, SETTINGS)
    first, second = payloads
    metafields = {m["key"]: m["value"] for m in second.post_actions[0]["variables"]["metafields"]}
    check("e-mail normalizado e usado como identificador", first.variables["identifier"] == {"email": "a@teste.com"})
    check("primeiro cliente fica com o telefone", first.variables["input"].get("phone") == "+5511999998888")
    check("segundo com o mesmo telefone fica sem, com aviso",
          "phone" not in second.variables["input"] and bool(second.warnings))
    check("telefone preservado no metafield", metafields.get("telefone") == "11999998888")
    check("CNPJ no metafield e tag de pessoa juridica",
          metafields.get("cpf_cnpj") == "11222333000181" and "pessoa-juridica" in second.variables["input"]["tags"])
    check("data de nascimento zerada nao vira metafield", "data_nascimento" not in metafields)
    check("newsletter gera aceite de marketing",
          any(a.get("mutation") == "customerEmailMarketingConsentUpdate" for a in first.post_actions))
    check("sem newsletter nao gera aceite",
          not any(a.get("mutation") == "customerEmailMarketingConsentUpdate" for a in second.post_actions))
    address = first.variables["input"]["addresses"][0]
    check("endereco com CEP formatado, UF e complemento",
          address["zip"] == "01001-000" and address["provinceCode"] == "SP" and address["address2"] == "apto 2 - Centro")

    leads = customers.leads(src, SETTINGS, used_phones)
    check("lead so para quem nao e cliente", len(leads) == 1 and leads[0].variables["input"]["email"] == "lead@teste.com")
    check("chave do lead nao expoe o e-mail", "@" not in leads[0].source_key)
    check("lead com telefone ja usado fica sem telefone", "phone" not in leads[0].variables["input"])
    for payload in payloads + leads:
        errors = schema_errors(payload)
        check(f"schema ok: {payload.source_key}", not errors, errors[:3])


def test_store_credits() -> None:
    print("\nCashback\n")
    data = {
        "customers": [{"id": 1}],
        "cashback_entries": [
            {"id": "a", "type": "credit", "status": "approved", "value": 10, "used": 2.5, "customer_id": 1,
             "expires_at": "2025-07-01 00:00:00", "release_at": "2025-05-01 00:00:00"},
            {"id": "b", "type": "credit", "status": "approved", "value": 5, "used": 0, "customer_id": 1,
             "expires_at": "2025-05-01 00:00:00", "release_at": None},
            {"id": "c", "type": "credit", "status": "approved", "value": 5, "used": 0, "customer_id": 1,
             "expires_at": "2025-08-01 00:00:00", "release_at": "2025-06-15 00:00:00"},
            {"id": "d", "type": "debit", "status": "approved", "value": 2.5, "used": 0, "customer_id": 1},
        ],
    }
    payloads, stats = customers.store_credits(FakeSource(data), SETTINGS, REFERENCE)
    ready = [p for p in payloads if p.status == "ready"]
    check("so o credito valido e liberado vira payload, com saldo = valor - usado",
          len(ready) == 1 and ready[0].variables["creditInput"]["creditAmount"]["amount"] == "7.50")
    check("data de expiracao preservada", ready[0].variables["creditInput"].get("expiresAt") == "2025-07-01T00:00:00-03:00")
    check("credito ainda nao liberado fica registrado como pulado",
          any(p.status == "skipped" and p.source_key == "cashback:c" for p in payloads))
    check("expirado nao gera payload, so estatistica",
          stats["credito expirado"] == 1 and not any(p.source_key == "cashback:b" for p in payloads))
    errors = schema_errors(ready[0])
    check("schema ok", not errors, errors[:3])


def test_discounts() -> None:
    print("\nDescontos\n")
    base = {"type": "coupon", "active": True, "value": "10.00", "value_type": "percentage",
            "date_from": "2025-01-01", "date_to": None, "created_at": "2025-01-01 00:00:00",
            "single_usage": False, "usage_limit": None, "min_purchase": None,
            "coupon_allow_free_freight": True, "is_free_freight": False, "entitled_product_ids": [],
            "entitled_category_ids": [], "prerequisite_product_ids": [], "prerequisite_quantity": 1,
            "zipcodes": []}

    def coupon(identifier, code=None, **changes):
        return {**base, "id": identifier, "name": f"Cupom {identifier}",
                "codes": [{"code": code or f"cupom{identifier}"}], **changes}

    data = {
        "products": [{"id": 7}],
        "categories": [],
        "discounts": [
            coupon(1), coupon(2, active=False), coupon(3, date_to="2025-03-01"), coupon(4, code="CUPOM1"),
            coupon(5, entitled_product_ids=[7]), coupon(6, entitled_product_ids=[999]),
            {**base, "id": 7, "type": "freight", "name": "Frete", "codes": [], "value": "100.00",
             "min_purchase": "300.00"},
            coupon(8, value="0.00", is_free_freight=True),
        ],
    }
    payloads, manual_items = marketing.discounts(FakeSource(data), SETTINGS, REFERENCE)
    by_key = {p.source_key: p for p in payloads}
    basic = by_key["discount:1"].variables["basicCodeDiscount"]
    check("10% vira percentage 0.1", basic["customerGets"]["value"] == {"percentage": 0.1})
    check("inativo pulado", by_key["discount:2"].skip_reason == "inativo na Bagy")
    check("vencido pulado", by_key["discount:3"].skip_reason == "vencido na Bagy")
    check("codigo repetido com outra caixa e pulado", by_key["discount:4"].status == "skipped")
    check("cupom de produto aponta para o produto",
          by_key["discount:5"].variables["basicCodeDiscount"]["customerGets"]["items"]
          == {"products": {"productsToAdd": ["bagy-ref:product:7"]}})
    check("cupom de produto inexistente nao vira desconto na loja toda", by_key["discount:6"].status == "skipped")
    check("frete automatico vai para o checklist", by_key["discount:7"].status == "skipped" and len(manual_items) == 1)
    free = by_key["discount:8"]
    check("cupom de frete gratis nao combina com outro desconto de frete",
          free.mutation == "discountCodeFreeShippingCreate"
          and free.variables["freeShippingCodeDiscount"]["combinesWith"]["shippingDiscounts"] is False)
    for key in ("discount:1", "discount:5", "discount:8"):
        errors = schema_errors(by_key[key])
        check(f"schema ok: {key}", not errors, errors[:3])


def test_vendors() -> None:
    print("\nFornecedores\n")
    src = FakeSource({"brands": [{"id": 1, "name": "Mad 4 Life", "slug": "mad-4-life"},
                                 {"id": 2, "name": "Mad4life", "slug": "mad4life"},
                                 {"id": 3, "name": "Outra Marca", "slug": "outra-marca"}]})
    check("sem TRANSFORM_VENDOR_NAMES as grafias ficam como na Bagy",
          set(catalog.vendors(src, SETTINGS).values()) == {"Mad 4 Life", "Mad4life", "Outra Marca"})
    settings = replace(SETTINGS, vendor_names=("Mad 4 Life",))
    names = catalog.vendors(src, settings)
    check("grafias da mesma marca viram um fornecedor so", names["Mad4life"] == names["Mad 4 Life"] == "Mad 4 Life")
    check("marca diferente nao e afetada", names["Outra Marca"] == "Outra Marca")
    check("URL antiga da marca aponta para o fornecedor unificado",
          content.path_map(src, settings, names).get("/mad4life") == "/collections/vendors?q=Mad%204%20Life")


def test_content() -> None:
    print("\nConteudo\n")
    hotsite = {
        "id": 30, "slug": "sobre", "name": "Sobre", "active": True,
        "content": {
            "content_for_index": ["html-1", "it-1", "ps-1"],
            "sections": {
                "html-1": {"type": "html", "settings": {"content": '<img src="media://files/a.png">'}},
                "it-1": {"type": "image-text", "settings": {"title": "Titulo"},
                         "blocks": [{"settings": {"title": "Bloco", "text": "Texto", "image": "media://files/b.jpg"}}]},
                "ps-1": {"type": "products-showcase", "settings": {}},
            },
        },
    }
    data = {
        "products": [{"id": 7, "slug": "produto-x"}],
        "categories": [{"id": 3, "slug": "kits", "name": "Kits"}],
        "brands": [],
        "posts": [{"id": 9, "slug": "post-y", "name": "Post Y", "description": "<p>oi</p>", "tags": "a, b",
                   "active": True, "created_at": "2025-01-01 10:00:00",
                   "image": {"src": "media://files/capa.jpg"}, "post_category_id": 1}],
        "post_categories": [{"id": 1, "slug": "energia-performance-natural", "name": "Energia & Performance Natural"}],
        "pages": [
            {"id": 20, "slug": "politica-de-privacidade", "name": "Privacidade", "description": "<p>texto</p>",
             "active": True, "template": "page"},
            {"id": 21, "slug": "blog", "name": "Blog", "active": True, "template": "page"},
        ],
        "hotsites": [hotsite],
        "redirects": [
            {"id": 1, "url_from": "/antigo-produto/p", "url_to": "https://loja.bagypro.com/produto-x"},
            {"id": 2, "url_from": "/receitas-velhas", "url_to": "https://loja.bagypro.com/receitas"},
        ],
        "menus": [{"id": 5, "name": "Principal", "handle": "principal", "values": [
            {"active": True, "menu_type": "category", "category_id": 3, "name": "Kits"},
            {"active": True, "menu_type": "page", "page_id": 21, "name": "Blog"},
            {"active": True, "menu_type": "custom_link", "link": "https://www.loja.com/kits", "name": "Link"},
        ]}],
    }
    src = FakeSource(data)
    mapping = content.path_map(src, SETTINGS)
    check("produto antigo -> /products", mapping.get("/produto-x") == "/products/produto-x")
    check("categoria do blog -> tag do blog",
          mapping.get("/energia-performance-natural") == "/blogs/blog/tagged/energia-performance-natural")
    check("politica -> /policies", mapping.get("/politica-de-privacidade") == "/policies/privacy-policy")

    redirects = {p.source_key: p for p in content.redirects(src, SETTINGS, mapping)}
    check("redirect da Bagy reescrito do bagypro para a Shopify",
          redirects["redirect:1"].variables["urlRedirect"] == {"path": "/antigo-produto/p", "target": "/products/produto-x"})
    check("destino inexistente vai para a home, com aviso",
          redirects["redirect:2"].variables["urlRedirect"]["target"] == "/" and bool(redirects["redirect:2"].warnings))

    body, warnings = content.render_hotsite(hotsite, 159387)
    check("media:// do editor visual vira URL do CDN",
          "https://cdn.dooca.store/159387/files/a.png" in body and "media://" not in body)
    check("bloco image-text renderizado com imagem e titulo",
          "https://cdn.dooca.store/159387/files/b.jpg" in body and "<h3>Bloco</h3>" in body)
    check("secao sem equivalente gera aviso", any("products-showcase" in w for w in warnings))

    pages = {p.source_key: p for p in content.pages(src, SETTINGS)}
    check("politica vira shopPolicyUpdate", pages["page:20"].mutation == "shopPolicyUpdate")
    check("pagina Blog da Bagy nao vira pagina", pages["page:21"].status == "skipped")

    article = content.articles(src, SETTINGS)[0].variables["article"]
    check("artigo com tags do post + categoria e imagem resolvida",
          article["tags"] == ["a", "b", "Energia & Performance Natural"]
          and article["image"]["url"] == "https://cdn.dooca.store/159387/files/capa.jpg")

    menu = content.menus(src, SETTINGS, mapping)[0]
    items = menu.variables["items"]
    check("menu: categoria -> COLLECTION, pagina Blog -> BLOG, link interno reescrito",
          items[0]["type"] == "COLLECTION" and items[1]["type"] == "BLOG"
          and items[2] == {"title": "Link", "type": "HTTP", "url": "/collections/kits"})

    for payload in [content.articles(src, SETTINGS)[0], menu, pages["page:20"],
                    *content.hotsite_pages(src, SETTINGS), *redirects.values()]:
        if payload.status == "ready":
            errors = schema_errors(payload)
            check(f"schema ok: {payload.source_key}", not errors, errors[:3])


def test_product_content() -> None:
    print("\nConteudo dos hotsites de produto (metaobjetos)\n")
    import json

    from bagy_transform.entities import catalog, product_content
    from bagy_transform.richtext import html_to_rich_text, nutrition_table

    rich = json.loads(html_to_rich_text('<p>Porção: <b>10 g</b></p><ul class="x"><li>Um</li>'
                                        '<li>Dois <a href="/p">link</a></li></ul><h2>Dicas</h2><style>.a{}</style>'))
    kinds = [child["type"] for child in rich["children"]]
    check("rich text: paragrafo com negrito, lista, titulo; style some",
          kinds == ["paragraph", "list", "heading"]
          and rich["children"][0]["children"][1] == {"type": "text", "value": "10 g", "bold": True}, rich)
    check("rich text: link dentro do item da lista",
          rich["children"][1]["children"][1]["children"][1]["type"] == "link", rich["children"][1])

    table = nutrition_table('<br><p>Porções por embalagem: 40 <br>Porção: 5g (Uma colher de chá)</p><table><thead>'
                            '<tr><th></th><th>100g</th><th>5g</th><th>%VD*</th></tr></thead><tbody><tr>'
                            '<td>Valor Energ&eacute;tico (kcal)</td><td>40</td><td>2</td><td>0</td></tr></tbody>'
                            '</table><p class="nota">*%VD com base<br>Ingredientes: <b>Guaraná</b>.</p>'
                            '<style>.t{}</style>')
    check("tabela nutricional: colunas, linhas, porcao, porcoes e observacao",
          table["colunas"] == ["100g", "5g", "%VD*"]
          and table["linhas"] == [{"nutriente": "Valor Energético (kcal)", "valores": ["40", "2", "0"]}]
          and table["porcao"] == "5g (Uma colher de chá)" and table["porcoes_por_embalagem"] == "40"
          and "Ingredientes: Guaraná." in (table["observacao"] or ""), table)

    hotsite = {"id": 40, "slug": "guarana-pdp", "name": "Guarana PDP", "active": True, "content": {
        "content_for_index": ["a", "b", "c", "d", "e"],
        "sections": {
            "a": {"type": "image-text", "blocks": [
                {"settings": {"title": "Tabela Nutricional", "text": "<p>Porção: 5g</p><table><tr><th></th>"
                              "<th>100g</th></tr><tr><td>Sódio (mg)</td><td>3</td></tr></table>"}},
                {"settings": {"title": "COMO CONSUMIR", "text": "<ul><li>5g por dia</li></ul>"}}]},
            "b": {"type": "image-text", "blocks": [
                {"settings": {"title": "Ingredientes<br><br>", "text": "<p>100% guaraná</p>"}},
                {"settings": {"title": "Insira um título", "text": "Insira um subtítulo"}}]},
            "c": {"type": "banners-commercial", "blocks": [
                {"settings": {"image": {"src": "media://files/selo-vegan.jpg?v=1"}}},
                {"settings": {"title": "Peso líquido", "description": "225g", "image": {"src": None}}}]},
            "d": {"type": "reviews", "blocks": [
                {"settings": {"client": "Carla M.", "description": "Muita energia", "star_rating": "6",
                              "image": {"src": "media://files/carla.jpg"}}}]},
            "e": {"type": "image-text", "blocks": [
                {"settings": {"title": "O que é?", "text": "<p>Livre</p>", "image": {"src": "media://files/x.jpg"}}}]},
        }}}
    data = {
        "products": [{"id": 8, "slug": "guarana-250g", "name": "Guaraná 250g", "active": True, "hotsite_id": 40},
                     {"id": 7, "slug": "guarana-100g", "name": "Guaraná 100g", "active": True, "hotsite_id": 40}],
        "hotsites": [hotsite], "pages": [], "features": [], "categories": [], "brands": [], "posts": [],
        "post_categories": [], "redirects": [],
        "menus": [{"id": 1, "name": "M", "handle": "m", "values": [
            {"active": True, "menu_type": "hotsite", "hotsite_id": 40, "name": "Guaraná"}]}],
    }
    src = FakeSource(data)
    built = product_content.build(src, SETTINGS)
    contents = built["product_content"]
    keys = [value["key"] for value in contents[0].variables["metafields"]]
    check("um metafieldsSet por produto do hotsite, com os 6 campos",
          len(contents) == 2 and keys == ["tabela_nutricional", "modo_de_uso", "ingredientes", "selos",
                                          "especificacoes", "depoimentos"]
          and contents[0].variables["metafields"][0]["ownerId"] == "bagy-ref:product:7", (len(contents), keys))
    types = sorted(p.variables["handle"]["type"] for p in built["metaobject"])
    check("metaobjetos: tabela, especificacoes e depoimentos (selo nao e mais metaobjeto; placeholder fora)",
          types == ["depoimentos", "especificacoes", "tabela_nutricional"] and len(built["file"]) == 2, types)
    by_type = {p.variables["handle"]["type"]: {f["key"]: f["value"] for f in p.variables["metaobject"]["fields"]}
               for p in built["metaobject"]}
    check("tabela: linhas como lista de texto 'Nutriente | valor'",
          json.loads(by_type["tabela_nutricional"]["linhas"]) == ["Sódio (mg) | 3"], by_type["tabela_nutricional"])
    check("especificacoes em listas paralelas (titulos e conteudos)",
          json.loads(by_type["especificacoes"]["titulos"]) == ["Peso líquido"]
          and json.loads(by_type["especificacoes"]["conteudos"]) == ["225g"], by_type["especificacoes"])
    reviews = by_type["depoimentos"]
    check("depoimentos em listas: clientes, textos, notas (6 vira 5, com aviso) e fotos",
          json.loads(reviews["clientes"]) == ["Carla M."] and json.loads(reviews["textos"]) == ["Muita energia"]
          and json.loads(reviews["notas"])[0]["value"] == "5.0"
          and json.loads(reviews["fotos"])[0].startswith("bagy-ref:file:")
          and any("acima de 5" in w for w in contents[0].warnings), reviews)
    seals = next(value for value in contents[0].variables["metafields"] if value["key"] == "selos")
    check("selos: lista de imagens direto no produto",
          seals["type"] == "list.file_reference" and len(json.loads(seals["value"])) == 1
          and json.loads(seals["value"])[0].startswith("bagy-ref:file:"), seals)
    check("secao livre avisada para o tema", any("ficam para o tema" in w for w in contents[0].warnings))
    check("hotsite de produto nao vira pagina", content.hotsite_pages(src, SETTINGS)[0].status == "skipped")
    check("URL antiga do hotsite leva ao produto (o primeiro ativo)",
          content.path_map(src, SETTINGS).get("/guarana-pdp") == "/products/guarana-100g")
    menu_item = content.menus(src, SETTINGS, content.path_map(src, SETTINGS))[0].variables["items"][0]
    check("menu que apontava para o hotsite aponta para o produto",
          menu_item["type"] == "PRODUCT" and menu_item["resourceId"] == "bagy-ref:product:7", menu_item)
    definitions = {p.source_key: p for p in catalog.metafield_definitions(src)}
    selos = definitions["product.custom.selos"].variables["definition"]
    reviews_definition = definitions["product.custom.depoimentos"].variables["definition"]
    check("definicoes do produto: selos lista de imagens; depoimentos valida pelo ID da definicao do metaobjeto; "
          "conteudo_pagina saiu",
          selos["type"] == "list.file_reference"
          and selos["validations"] == [{"name": "file_type_options", "value": '["Image"]'}]
          and reviews_definition["type"] == "metaobject_reference"
          and reviews_definition["validations"] == [{"name": "metaobject_definition_id",
                                                     "value": "bagy-ref:metaobject_definition:depoimentos"}]
          and "product.custom.conteudo_pagina" not in definitions, (selos, reviews_definition))
    for payload in [*built["metaobject_definition"], *built["metaobject"], *built["file"], *contents,
                    definitions["product.custom.selos"], definitions["product.custom.depoimentos"],
                    definitions["product.custom.modo_de_uso"]]:
        errors = schema_errors(payload)
        check(f"schema ok: {payload.source_key}", not errors, errors[:3])


def test_phones() -> None:
    print("\nTelefones (a Shopify valida o numero)\n")
    from bagy2shopify.transform import e164_br

    cases = {
        "(11) 98888-7777": "+5511988887777",      # celular atual
        "11 8888-7777": "+5511988887777",         # celular antigo sem o 9: ganha o 9
        "+55 (21) 7777-6666": "+5521977776666",   # com codigo do pais, celular antigo
        "011 3333-4444": "+551133334444",         # fixo com zero na frente
        "(20) 98888-7777": None,                  # DDD inexistente
        "(11) 38888-7777": None,                  # 11 digitos que nao comecam com 9
        "8888-7777": None,                        # sem DDD
        "": None,
    }
    for raw, expected in cases.items():
        check(f"telefone {raw!r} -> {expected}", e164_br(raw) == expected, e164_br(raw))


def test_emails() -> None:
    print("\nE-mails com dominio incompleto\n")
    from bagy_transform.entities.customers import fix_email

    cases = {
        "Fulano99@gail": ("fulano99@gmail.com", True),
        "fulano@icloud": ("fulano@icloud.com", True),
        "fulano_x@hotmail": ("fulano_x@hotmail.com", True),
        "fulano@hotmail.com": ("fulano@hotmail.com", False),
        "fulano@empresa": ("fulano@empresa", False),       # dominio desconhecido: nao inventa
        "": ("", False),
    }
    for raw, expected in cases.items():
        check(f"e-mail {raw!r} -> {expected[0]!r}", fix_email(raw) == expected, fix_email(raw))


def test_schema_validator() -> None:
    print("\nValidador de schema\n")
    errors, _ = VALIDATOR.validate("menuCreate", {"handle": "x", "items": []})
    check("argumento obrigatorio ausente e detectado", any("title" in e for e in errors), errors)
    errors, _ = VALIDATOR.validate("urlRedirectCreate", {"urlRedirect": {"path": "/a", "target": "/b", "campoFalso": 1}})
    check("campo inexistente e detectado", any("campoFalso" in e for e in errors), errors)
    errors, _ = VALIDATOR.validate("orderCreate", {"order": {"currency": "REAL", "lineItems": [{"quantity": 1}]}})
    check("valor fora do enum e detectado", any("CurrencyCode" in e for e in errors), errors)
    errors, _ = VALIDATOR.validate("storeCreditAccountCredit", {"id": "x", "creditInput": {
        "creditAmount": {"amount": "1.00", "currencyCode": "BRL"}, "expiresAt": "01/07/2025"}})
    check("data fora do formato ISO e detectada", any("expiresAt" in e for e in errors), errors)
    errors, _ = VALIDATOR.validate("blogCreate", {"blog": {"title": "Blog"}})
    check("payload minimo valido passa", not errors, errors)


def test_links_and_approximate_redirects() -> None:
    print("\nLinks internos e redirects aproximados\n")
    from bagy_transform.common import rewrite_links

    mapping = {"/guarana-cipo": "/collections/guarana-cipo",
               "/curcuma-selvagem-em-po-45g-mad": "/products/curcuma-selvagem-em-po-45g-mad"}
    hosts = {"www.loja.com", "loja.com"}
    original = ('<a href="https://www.loja.com/guarana-cipo?utm_source=bagy&amp;utm_medium=blog">g</a> '
                '<a href="https://externo.com/guarana-cipo">x</a> <a href="mailto:a@b.c">m</a> '
                '<a href="/curcuma-selvagem-em-po-45g-mad#topo">c</a>')
    rewritten, count = rewrite_links(original, mapping, hosts)
    check("link interno aponta direto para a URL nova, mantendo a query",
          'href="/collections/guarana-cipo?utm_source=bagy&amp;utm_medium=blog"' in rewritten)
    check("link externo e mailto ficam intactos",
          'href="https://externo.com/guarana-cipo"' in rewritten and 'href="mailto:a@b.c"' in rewritten)
    check("link relativo com ancora reescrito",
          'href="/products/curcuma-selvagem-em-po-45g-mad#topo"' in rewritten)
    check("so os links internos contam como reescritos", count == 2)

    src = FakeSource({
        "products": [{"id": 1, "slug": "curcuma-selvagem-em-po-45g-mad"}],
        "redirects": [{"id": 1, "url_from": "/curcuma-em-po-0012079/p",
                       "url_to": "https://loja.bagypro.com/curcuma-selvagem-em-po-44g-mad"}],
    })
    redirect = content.redirects(src, SETTINGS, content.path_map(src, SETTINGS))[0]
    check("destino renomeado (44g -> 45g) vai para o caminho mais parecido, com aviso",
          redirect.variables["urlRedirect"]["target"] == "/products/curcuma-selvagem-em-po-45g-mad"
          and any("mais parecido" in w for w in redirect.warnings))


def main() -> int:
    test_phones()
    test_emails()
    test_schema_validator()
    test_orders()
    test_customers()
    test_store_credits()
    test_discounts()
    test_vendors()
    test_content()
    test_product_content()
    test_links_and_approximate_redirects()
    print("\n" + ("Tudo passou." if not FAILURES else f"{FAILURES} falha(s)."))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
