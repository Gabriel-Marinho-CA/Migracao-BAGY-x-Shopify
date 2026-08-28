"""Gera os mocks da API de pedidos da Bagy em JSONs validos e paginados.

O `mocks.json` original nao e JSON valido: sao tres documentos concatenados com
texto em portugues no meio, e o primeiro bloco tem chaves desbalanceadas. Este
script reconstroi aquele conteudo em arquivos separados e ainda adiciona pedidos
dummy para exercitar a paginacao e os varios status.

Saida (em ./mocks):
    orders_page_1.json .. orders_page_3.json   -> resposta do endpoint de listagem
    order_<id>.json                            -> resposta do endpoint de detalhe

Uso:
    python tools/gen_mocks.py
"""

from __future__ import annotations

import json
from pathlib import Path
from decimal import Decimal

OUT_DIR = Path(__file__).resolve().parent.parent / "mocks"
PAGE_LIMIT = 3
STORE_ID = "1098664"


# --------------------------------------------------------------------------- #
# Catalogos base
# --------------------------------------------------------------------------- #

PRODUCTS = {
    "351": dict(name="tenis azul", ref="ref351", price="900.00", cost="333.33",
                weight="100", length="28", width="15", height="10",
                category=("9", "Outlet"), slug="tenis-azul"),
    "88": dict(name="notebook gamer 16gb", ref="ref88", price="35999.00", cost="24500.00",
               weight="3200", length="45", width="32", height="8",
               category=("4", "Informatica"), slug="notebook-gamer-16gb"),
    "92": dict(name="tv oled 65 polegadas", ref="ref92", price="59900.00", cost="41000.00",
               weight="24000", length="150", width="95", height="20",
               category=("6", "Eletronicos"), slug="tv-oled-65-polegadas"),
    "412": dict(name="camiseta preta basica", ref="ref412", price="89.90", cost="28.00",
                weight="180", length="30", width="22", height="3",
                category=("2", "Vestuario"), slug="camiseta-preta-basica"),
    "508": dict(name="bone vermelho aba reta", ref="ref508", price="59.90", cost="19.50",
                weight="120", length="25", width="20", height="12",
                category=("3", "Acessorios"), slug="bone-vermelho-aba-reta"),
    "233": dict(name="mochila urbana 25l", ref="ref233", price="249.90", cost="98.00",
                weight="750", length="48", width="32", height="18",
                category=("3", "Acessorios"), slug="mochila-urbana-25l"),
    "601": dict(name="kit meia 3 pares", ref="ref601", price="39.90", cost="12.40",
                weight="150", length="20", width="14", height="6",
                category=("2", "Vestuario"), slug="kit-meia-3-pares"),
}

CUSTOMERS = {
    "21": dict(name="Danilo Carlos Ribeiro", email="danilo.carlos123@bagy.net.br",
               cpf="00474780040", rg="364787958", phone="1433334444", cell="18999898989",
               address="Rua Cristiano Olsen", number="1067", complement="apto 44",
               neighborhood="Jardim Sumare", city="Aracatuba", state="SP",
               zip_code="16015-244", recipient="Danilo O", created="2024-03-08 13:39:08",
               address_id="167"),
    "5": dict(name="Marina Souza Lima", email="marina.lima@example.com",
              cpf="11144477735", rg="221334455", phone="3133334444", cell="31988887777",
              address="Rua da Bahia", number="1200", complement="sala 5",
              neighborhood="Centro", city="Belo Horizonte", state="MG",
              zip_code="30160-011", recipient="Marina S Lima", created="2020-10-02 09:12:44",
              address_id="41"),
    "1": dict(name="Roberto Alves Pereira", email="roberto.pereira@example.com",
              cpf="98765432100", rg="998877665", phone="4132221111", cell="41999996666",
              address="Avenida Sete de Setembro", number="3400", complement="",
              neighborhood="Reboucas", city="Curitiba", state="PR",
              zip_code="80230-010", recipient="Roberto A Pereira", created="2019-05-20 18:03:10",
              address_id="15"),
    "34": dict(name="Juliana Ferreira Costa", email="juliana.costa@example.com",
               cpf="52998224725", rg="445566778", phone="8134445555", cell="81988883333",
               address="Rua do Sol", number="45", complement="apto 302",
               neighborhood="Boa Vista", city="Recife", state="PE",
               zip_code="50060-060", recipient="Juliana F Costa", created="2024-11-11 21:40:02",
               address_id="204"),
    "47": dict(name="Thiago Nunes Barbosa", email="thiago.barbosa@example.com",
               cpf="16899549140", rg="112233445", phone="5133332222", cell="51999994444",
               address="Avenida Ipiranga", number="6681", complement="bloco B",
               neighborhood="Partenon", city="Porto Alegre", state="RS",
               zip_code="90619-900", recipient="Thiago N Barbosa", created="2025-01-30 11:22:33",
               address_id="231"),

    # --- casos de borda, para exercitar a validacao de documento ---
    "88": dict(name="Fernanda Ribeiro Alves", email="compras@turboindustria.com.br",
               cnpj="11222333000181", company_name="Turbo Industria e Comercio LTDA",
               state_inscription="110042490114", rg="", cpf="",
               phone="1133224455", cell="11988776655",
               address="Avenida Paulista", number="1578", complement="conjunto 142",
               neighborhood="Bela Vista", city="Sao Paulo", state="SP",
               zip_code="01310-200", recipient="Fernanda R Alves",
               created="2025-06-12 10:05:00", address_id="310"),
    "92": dict(name="Carlos Eduardo Menezes", email="carlos.menezes@example.com",
               cpf="45678912300", rg="556677889", phone="7132221100", cell="71988884444",
               address="Rua Chile", number="212", complement="",
               neighborhood="Comercio", city="Salvador", state="BA",
               zip_code="40020-000", recipient="Carlos E Menezes",
               created="2025-07-01 14:30:00", address_id="322"),
    # Cliente inedito, para provar que a criacao ja grava o CPF no registro
    # do cliente (metafield), nao so no pedido.
    "101": dict(name="Renata Aparecida Souza", email="renata.souza@example.com",
                cpf="39053344705", rg="223344556", phone="2133445566", cell="21988997766",
                address="Rua Voluntarios da Patria", number="340", complement="sala 12",
                neighborhood="Botafogo", city="Rio de Janeiro", state="RJ",
                zip_code="22270-000", recipient="Renata A Souza",
                created="2025-08-25 08:00:00", address_id="401"),
    "95": dict(name="Beatriz Lopes Martins", email="beatriz.martins@example.com",
               cpf="", rg="", phone="6232221010", cell="62988885555",
               address="Avenida Goias", number="900", complement="apto 71",
               neighborhood="Setor Central", city="Goiania", state="GO",
               zip_code="74005-010", recipient="Beatriz L Martins",
               created="2025-07-20 09:15:00", address_id="335"),
}

STATUS_META = {
    "A ENVIAR": ("1", "open", "#CAFAD1"),
    "FINALIZADO": ("69", "closed", "#85CC8D"),
    "CANCELADO": ("5", "canceled", "#F5C6C6"),
    "AGUARDANDO PAGAMENTO": ("2", "open", "#FDF2C0"),
    "ENTREGUE": ("70", "closed", "#7FBF87"),
    "EM SEPARACAO": ("3", "open", "#D6E4FF"),
}


# --------------------------------------------------------------------------- #
# Specs dos pedidos (compactas -> expandidas para o formato da Bagy)
# --------------------------------------------------------------------------- #
# items: lista de (product_id, quantidade)
# rate:  payment_method_rate -> positivo = acrescimo (juros), negativo = desconto
# coupon: (codigo, valor_desconto) ou None

ORDER_SPECS = [
    dict(id="5", date="2020-12-07", hour="11:55:22", status="A ENVIAR", customer="5",
         items=[("88", 1)], shipment="Correios Expresso", shipment_value="28.35",
         integrator="Frete Facil", coupon=("natal25", "8999.75"), rate="0.00",
         payment_method="Boleto - Yapay", payment_type="billet", payment_method_id="80",
         installment="1", delivery_time="3", eta="2020-12-22", has_invoice="1",
         access_code="DB91EEA5D671CA2", session="rdlq7ogripa61r33p75bei8cj2",
         dc_id="611", modified="2020-12-21 11:21:52",
         store_note="07/12/2020 11:55:22 Pedido em 1 vez de R$ 27,027.60 atraves do Boleto - Yapay - Boleto"),

    dict(id="15", date="2021-02-10", hour="11:28:21", status="FINALIZADO", customer="1",
         items=[("92", 1)], shipment="Correios Expresso", shipment_value="38.91",
         integrator="Frete Facil", coupon=None, rate="2996.95",
         payment_method="Boleto - Yapay", payment_type="billet", payment_method_id="80",
         installment="1", delivery_time="3", eta="2021-02-15", has_invoice="0",
         access_code="38D071AEFEF4960", session="k8ku3icuvb5uge2qj7u8gbtli6",
         dc_id="611", modified="2021-05-06 10:38:45",
         store_note=("10/02/2021 11:28:28 Pedido em 1 vez de R$ 62,935.86 atraves do Boleto - Yapay"
                     " - Boleto Link da transacao:"
                     " https://intermediador.yapay.com.br/orders/billet/17471d7cd90f7d3jk54643e1da0f15"),
         transaction_url="https://intermediador.yapay.com.br/orders/billet/8eb117cdada159753468efe9cd45157c7"),

    dict(id="1167", date="2025-02-06", hour="09:46:09", status="A ENVIAR", customer="21",
         items=[("351", 1)], shipment="PAC Bagy", shipment_value="15.39",
         integrator="Correios", coupon=None, rate="-90.00",
         payment_method="Pix - Vindi", payment_type="pix", payment_method_id="10545",
         installment="0", delivery_time="5", eta="2025-02-13", has_invoice="0",
         access_code="2EA95D335ECB6F9", session="", dc_id="799",
         modified="2025-02-06 09:57:58", external_code="Bagy__1163-1",
         store_note="Criado a partir do pedido: 1163\n\nCD ref: Rio preto - SP\n",
         payment_entry=dict(id="507", method="Vale postal", unique_number="Bagy__505__22861-1")),

    dict(id="1168", date="2025-03-14", hour="15:22:10", status="FINALIZADO", customer="5",
         items=[("412", 2), ("601", 1)], shipment="SEDEX Bagy", shipment_value="22.40",
         integrator="Correios", coupon=None, rate="0.00",
         payment_method="Cartao de Credito - Vindi", payment_type="credit_card",
         payment_method_id="10546", installment="3", delivery_time="2", eta="2025-03-18",
         has_invoice="1", access_code="7C1B0A4E9F2D385", session="qm2v8t7xk1p0zj4n6c9h3s5db1",
         dc_id="799", modified="2025-03-18 08:41:02",
         sending_code="BR123456789BR", delivered="1",
         store_note="14/03/2025 15:22:31 Pedido em 3x de R$ 242,10 atraves do Cartao de Credito - Vindi",
         payment_entry=dict(id="612", method="Cartao de Credito", unique_number="Bagy__612__23110-1")),

    dict(id="1172", date="2025-03-20", hour="10:05:44", status="CANCELADO", customer="1",
         items=[("508", 1)], shipment="PAC Bagy", shipment_value="18.70",
         integrator="Correios", coupon=None, rate="0.00",
         payment_method="Boleto - Vindi", payment_type="billet", payment_method_id="10544",
         installment="1", delivery_time="6", eta="2025-03-28", has_invoice="0",
         access_code="9AD3F70C2E8B146", session="t4r8w2q6y0u3i7o1p5a9s2d6f0",
         dc_id="799", modified="2025-03-27 16:10:55", payment_date="0000-00-00",
         store_note="Pedido cancelado por falta de pagamento do boleto apos 7 dias."),

    dict(id="1180", date="2025-04-02", hour="08:12:00", status="AGUARDANDO PAGAMENTO",
         customer="34", items=[("233", 1), ("412", 1)], shipment="SEDEX Bagy",
         shipment_value="31.20", integrator="Correios", coupon=None, rate="0.00",
         payment_method="Boleto - Vindi", payment_type="billet", payment_method_id="10544",
         installment="1", delivery_time="2", eta="2025-04-07", has_invoice="0",
         access_code="4E82BC15A9D703F", session="z1x3c5v7b9n2m4k6j8h0g2f4d6",
         dc_id="799", modified="2025-04-02 08:12:41", payment_date="0000-00-00",
         has_payment="0",
         store_note="02/04/2025 08:12:19 Pedido em 1 vez de R$ 371,00 atraves do Boleto - Vindi"),

    dict(id="1185", date="2025-04-11", hour="17:45:31", status="A ENVIAR", customer="47",
         items=[("601", 3)], shipment="Retirar na loja", shipment_value="0.00",
         integrator="", coupon=None, rate="-11.97",
         payment_method="Pix - Vindi", payment_type="pix", payment_method_id="10545",
         installment="0", delivery_time="0", eta="2025-04-12", has_invoice="0",
         access_code="6BF091D3E7A2C58", session="l9k7j5h3g1f8d6s4a2q0w8e6r4",
         dc_id="799", modified="2025-04-11 17:52:08", pickup=True,
         store_note="Retirada na loja fisica - Rio preto - SP",
         payment_entry=dict(id="701", method="Pix", unique_number="Bagy__701__24001-1")),

    dict(id="1190", date="2025-04-25", hour="12:00:00", status="FINALIZADO", customer="21",
         items=[("351", 1), ("508", 2)], shipment="PAC Bagy", shipment_value="24.50",
         integrator="Correios", coupon=("outono10", "101.98"), rate="0.00",
         payment_method="Cartao de Credito - Vindi", payment_type="credit_card",
         payment_method_id="10546", installment="2", delivery_time="5", eta="2025-05-02",
         has_invoice="1", access_code="0C74AE28B6F91D3", session="p2o4i6u8y0t1r3e5w7q9a1s3d5",
         dc_id="799", modified="2025-05-02 09:15:20",
         sending_code="BR987654321BR", delivered="1",
         store_note="25/04/2025 12:00:44 Pedido em 2x de R$ 942,32 atraves do Cartao de Credito - Vindi",
         payment_entry=dict(id="744", method="Cartao de Credito", unique_number="Bagy__744__24210-1")),

    # ----------------------------------------------------------------------- #
    # Lote 2: exercita o que so passou a existir depois das melhorias -
    # fulfillment ja na criacao (locationId agora resolve), CNPJ, documento
    # invalido e cliente sem documento.
    # ----------------------------------------------------------------------- #

    # CNPJ + fulfillment na criacao. Pessoa juridica com inscricao estadual.
    dict(id="2001", date="2025-06-18", hour="10:32:14", status="FINALIZADO", customer="88",
         items=[("92", 1), ("233", 2)], shipment="SEDEX Bagy", shipment_value="88.40",
         integrator="Correios", coupon=None, rate="0.00",
         payment_method="Boleto - Vindi", payment_type="billet", payment_method_id="10544",
         installment="1", delivery_time="4", eta="2025-06-24", has_invoice="1",
         access_code="A1B2C3D4E5F6071", session="e5r7t9y1u3i5o7p9a1s3d5f7g9",
         dc_id="799", modified="2025-06-24 16:20:11",
         sending_code="BR555444333BR", delivered="1",
         store_note="18/06/2025 10:32:40 Pedido em 1 vez de R$ 60.488,20 atraves do Boleto - Vindi",
         payment_entry=dict(id="810", method="Boleto", unique_number="Bagy__810__25001-1")),

    # CPF invalido: a Shopify recusa o TAX_CREDENTIAL_BR, mas o pedido e os
    # customAttributes precisam entrar mesmo assim.
    dict(id="2002", date="2025-07-03", hour="16:48:02", status="A ENVIAR", customer="92",
         items=[("412", 3)], shipment="PAC Bagy", shipment_value="19.90",
         integrator="Correios", coupon=("julho15", "40.46"), rate="0.00",
         payment_method="Pix - Vindi", payment_type="pix", payment_method_id="10545",
         installment="0", delivery_time="7", eta="2025-07-12", has_invoice="0",
         access_code="F7E6D5C4B3A2190", session="q1w2e3r4t5y6u7i8o9p0a1s2d3",
         dc_id="799", modified="2025-07-03 16:52:30",
         store_note="03/07/2025 16:48:20 Pedido em 1 vez de R$ 249,14 atraves do Pix - Vindi",
         payment_entry=dict(id="822", method="Pix", unique_number="Bagy__822__25010-1")),

    # Cliente sem CPF nenhum: nao pode gerar erro, so um aviso.
    dict(id="2003", date="2025-07-22", hour="11:05:47", status="EM SEPARACAO", customer="95",
         items=[("508", 1), ("601", 2)], shipment="PAC Bagy", shipment_value="21.30",
         integrator="Correios", coupon=None, rate="-7.99",
         payment_method="Pix - Vindi", payment_type="pix", payment_method_id="10545",
         installment="0", delivery_time="6", eta="2025-07-30", has_invoice="0",
         access_code="1122334455667788"[:15], session="m9n8b7v6c5x4z3l2k1j0h9g8f7",
         dc_id="799", modified="2025-07-22 11:10:03",
         store_note="22/07/2025 11:05:58 Pedido em 1 vez de R$ 153,01 atraves do Pix - Vindi",
         payment_entry=dict(id="835", method="Pix", unique_number="Bagy__835__25022-1")),

    # ENTREGUE com cupom e juros ao mesmo tempo: o desconto unico da Shopify
    # tem de somar tudo, e o acrescimo vira linha separada.
    dict(id="2004", date="2025-08-05", hour="09:20:33", status="ENTREGUE", customer="47",
         items=[("351", 1), ("412", 1)], shipment="SEDEX Bagy", shipment_value="27.60",
         integrator="Correios", coupon=("agosto20", "197.98"), rate="79.19",
         payment_method="Cartao de Credito - Vindi", payment_type="credit_card",
         payment_method_id="10546", installment="4", delivery_time="3", eta="2025-08-11",
         has_invoice="1", access_code="9F8E7D6C5B4A321", session="z9x8c7v6b5n4m3l2k1j0h9g8f7",
         dc_id="799", modified="2025-08-11 14:02:55",
         sending_code="BR777888999BR", delivered="1",
         store_note="05/08/2025 09:20:51 Pedido em 4x de R$ 898,71 atraves do Cartao de Credito - Vindi",
         payment_entry=dict(id="848", method="Cartao de Credito", unique_number="Bagy__848__25040-1")),

    # Regressao: FINALIZADO + juros de parcelamento. A linha de acrescimo ganha
    # um fulfillment order proprio; sem a varredura pos-criacao o pedido fica
    # PARTIALLY_FULFILLED para sempre.
    dict(id="2005", date="2025-08-14", hour="13:41:19", status="FINALIZADO", customer="34",
         items=[("233", 1), ("601", 1)], shipment="SEDEX Bagy", shipment_value="24.80",
         integrator="Correios", coupon=None, rate="28.98",
         payment_method="Cartao de Credito - Vindi", payment_type="credit_card",
         payment_method_id="10546", installment="3", delivery_time="3", eta="2025-08-19",
         has_invoice="1", access_code="5A4B3C2D1E0F9A8", session="a1b2c3d4e5f6g7h8i9j0k1l2m3",
         dc_id="799", modified="2025-08-19 10:11:22",
         sending_code="BR111222333BR", delivered="1",
         store_note="14/08/2025 13:41:40 Pedido em 3x de R$ 343,58 atraves do Cartao de Credito - Vindi",
         payment_entry=dict(id="861", method="Cartao de Credito", unique_number="Bagy__861__25055-1")),

    # ENTREGUE com cupom + juros + retirada na loja, para fechar os cruzamentos.
    dict(id="2006", date="2025-08-20", hour="15:02:08", status="ENTREGUE", customer="88",
         items=[("412", 2), ("508", 1)], shipment="Retirar na loja", shipment_value="0.00",
         integrator="", coupon=("agosto10", "23.97"), rate="10.79",
         payment_method="Cartao de Credito - Vindi", payment_type="credit_card",
         payment_method_id="10546", installment="2", delivery_time="0", eta="2025-08-21",
         has_invoice="1", access_code="8B7C6D5E4F3A2B1", session="n1m2b3v4c5x6z7l8k9j0h1g2f3",
         dc_id="799", modified="2025-08-21 09:33:41", pickup=True,
         store_note="Retirada na loja fisica - Rio preto - SP",
         payment_entry=dict(id="874", method="Cartao de Credito", unique_number="Bagy__874__25060-1")),

    # Mesma forma do 2005, para provar a varredura pos-criacao ja corrigida:
    # tem de sair FULFILLED direto, sem passar pelo fix_fulfillment.
    dict(id="2007", date="2025-08-26", hour="08:55:12", status="FINALIZADO", customer="92",
         items=[("351", 1), ("601", 2)], shipment="SEDEX Bagy", shipment_value="26.10",
         integrator="Correios", coupon=None, rate="49.49",
         payment_method="Cartao de Credito - Vindi", payment_type="credit_card",
         payment_method_id="10546", installment="3", delivery_time="3", eta="2025-08-31",
         has_invoice="1", access_code="3C2B1A0F9E8D7C6", session="k1l2m3n4o5p6q7r8s9t0u1v2w3",
         dc_id="799", modified="2025-08-31 17:44:09",
         sending_code="BR444555666BR", delivered="1",
         store_note="26/08/2025 08:55:33 Pedido em 3x de R$ 1.055,39 atraves do Cartao de Credito - Vindi",
         payment_entry=dict(id="887", method="Cartao de Credito", unique_number="Bagy__887__25070-1")),

    # Cliente novo: prova que a criacao grava o CPF nos DOIS lugares - no pedido
    # (localizedFields) e no registro do cliente (metafield).
    dict(id="2008", date="2025-08-27", hour="10:15:00", status="A ENVIAR", customer="101",
         items=[("233", 1)], shipment="PAC Bagy", shipment_value="17.40",
         integrator="Correios", coupon=None, rate="-24.99",
         payment_method="Pix - Vindi", payment_type="pix", payment_method_id="10545",
         installment="0", delivery_time="4", eta="2025-09-02", has_invoice="0",
         access_code="7D6C5B4A3F2E1D0", session="r1e2n3a4t5a6s7o8u9z0a1b2c3",
         dc_id="799", modified="2025-08-27 10:18:44",
         store_note="27/08/2025 10:15:22 Pedido em 1 vez de R$ 242,31 atraves do Pix - Vindi",
         payment_entry=dict(id="901", method="Pix", unique_number="Bagy__901__25080-1")),
]


# --------------------------------------------------------------------------- #
# Montagem
# --------------------------------------------------------------------------- #

def _d(value: str) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def build_products_sold(spec: dict) -> tuple[list, Decimal, Decimal]:
    """Retorna (ProductsSold, partial_total, custo_total)."""
    sold = []
    partial = Decimal("0.00")
    cost_total = Decimal("0.00")
    line_id = int(spec["id"]) * 10

    for product_id, qty in spec["items"]:
        p = PRODUCTS[product_id]
        line_id += 1
        line_total = _d(p["price"]) * qty
        partial += line_total
        cost_total += _d(p["cost"]) * qty
        cat_id, cat_name = p["category"]
        img = (f"img_prod/{STORE_ID}/{p['slug'].replace('-', '_')}_{product_id}_1"
               f"_387b5f42afaac09b5647da7df330d40c.jpg")

        sold.append({"ProductsSold": {
            "product_kit_id": "0",
            "product_kit_id_kit": "0",
            "id_campaign": "",
            "product_id": product_id,
            "quantity": str(qty),
            "id": str(line_id),
            "order_id": spec["id"],
            "name": f"{p['name']} (Ref. {p['ref']})",
            "original_name": p["name"],
            "virtual_product": "0",
            "ean": "",
            "availability_days": "0",
            "availability": "",
            "Sku": [],
            "price": p["price"],
            "cost_price": p["cost"],
            "original_price": p["price"],
            "weight": p["weight"],
            "weight_cubic": "875",
            "brand": "",
            "model": "",
            "reference": p["ref"],
            "length": p["length"],
            "width": p["width"],
            "height": p["height"],
            "variant_id": "0",
            "additional_information": "",
            "text_variant": "",
            "warranty": "",
            "bought_together_id": "0",
            "ncm": "",
            "included_items": "",
            "release_date": "0000-00-00",
            "commissioner_value": "",
            "comissao": "0.00",
            "is_giveaway_by_coupon": "",
            "ProductSoldImage": [{
                "http": f"http://images.tcdn.com.br/img/{img}",
                "https": f"https://images.tcdn.com.br/img/{img}",
                "thumbs": {
                    size: {
                        "http": f"http://images.tcdn.com.br/img/img_prod/{STORE_ID}/{size}_{img.split('/')[-1]}",
                        "https": f"https://images.tcdn.com.br/img/img_prod/{STORE_ID}/{size}_{img.split('/')[-1]}",
                    } for size in ("30", "90", "180")
                },
            }],
            "Category": [{"id": cat_id, "name": cat_name, "main_category": "1"}],
            "is_giveaway": "",
            "BoughtTogether": [],
            "ProductSoldPackage": [],
            "ProductSoldCard": [],
            "url": {
                "http": f"http://danilooliveiraalbergardi.commercesuite.com.br/{cat_name.lower()}/{p['slug']}",
                "https": f"https://ecommerce.analisevirtual.com.br/{cat_name.lower()}/{p['slug']}",
            },
            "Discount": [],
            "Stock": {"id": "1", "name": "Loja"},
        }})

    return sold, partial, cost_total


def build_customer(customer_id: str, spec: dict) -> dict:
    c = CUSTOMERS[customer_id]
    return {
        "cnpj": c.get("cnpj", ""),
        "newsletter": "1",
        "created": c["created"],
        "terms": "0000-00-00 00:00:00",
        "id": customer_id,
        "name": c["name"],
        "registration_date": c["created"][:10],
        "rg": c.get("rg", ""),
        "cpf": c.get("cpf", ""),
        "phone": c["phone"],
        "cellphone": c["cell"],
        "birth_date": "0000-00-00",
        "gender": "0",
        "email": c["email"],
        "nickname": "",
        "token": spec["access_code"],
        "total_orders": "0",
        "observation": "",
        "type": "0",
        "foreign": "0",
        "company_name": c.get("company_name", ""),
        "state_inscription": "",
        "reseller": "0",
        "discount": "0.000",
        "blocked": "",
        "credit_limit": "0.00",
        "indicator_id": "0",
        "profile_customer_id": "1",
        "last_sending_newsletter": "0000-00-00",
        "last_purchase": spec["date"],
        "last_visit": spec["date"],
        "last_modification": "0000-00-00 00:00:00",
        "address": c["address"],
        "zip_code": c["zip_code"],
        "number": c["number"],
        "complement": c["complement"],
        "neighborhood": c["neighborhood"],
        "city": c["city"],
        "state": c["state"],
        "country": "",
        "modified": spec["modified"],
        "count_orders": "0",
        "Extensions": {
            "Profile": {"id": "1", "name": "Padrao", "approves_registration": "0"},
            "Profiles": [{
                "id": "1", "price_list_id": "0", "name": "Padrao",
                "approves_registration": "0", "show_price": "", "theme_id": "0",
                "selected": "1",
            }],
        },
        "CustomerAddresses": [{
            "CustomerAddress": {
                "id": c["address_id"],
                "customer_id": customer_id,
                "address": c["address"],
                "number": c["number"],
                "complement": c["complement"],
                "neighborhood": c["neighborhood"],
                "city": c["city"],
                "state": c["state"],
                "zip_code": c["zip_code"],
                "country": "",
                "type": "1",
                "active": "1",
                "description": c["zip_code"].replace("-", ""),
                "recipient": c["recipient"],
                "type_delivery": "1",
                "not_list": "0",
            }
        }],
    }


def build_order(spec: dict) -> dict:
    """Expande uma spec para o formato completo de pedido da Bagy."""
    sold, partial, cost_total = build_products_sold(spec)

    coupon = spec.get("coupon")
    coupon_value = _d(coupon[1]) if coupon else Decimal("0.00")
    rate = _d(spec["rate"])
    shipment_value = _d(spec["shipment_value"])
    discount = Decimal("0.00")

    total = partial - discount - coupon_value + shipment_value + rate

    status = spec["status"]
    status_id, status_type, background = STATUS_META[status]
    is_paid = status in ("A ENVIAR", "FINALIZADO")

    order = {
        "status": status,
        "id": spec["id"],
        "date": spec["date"],
        "hour": spec["hour"],
        "customer_id": spec["customer"],
        "partial_total": _money(partial),
        "taxes": "0.00",
        "discount": _money(discount),
        "point_sale": "LOJA VIRTUAL",
        "shipment": spec["shipment"],
        "shipment_value": _money(shipment_value),
        "shipment_date": "",
        "delivered": spec.get("delivered", ""),
        "delivered_status": "",
        "shipping_cancelled": "",
        "store_note": spec.get("store_note", ""),
        "customer_note": spec.get("customer_note", ""),
        "partner_id": "",
        "discount_coupon": f"{coupon[0]}/{coupon[1]}" if coupon else "",
        "client_ip": "",
        "payment_method_rate": _money(rate),
        "installment": spec["installment"],
        "value_1": "0.00",
        "sending_code": spec.get("sending_code", ""),
        "sending_date": "0000-00-00",
        "billing_address": "",
        "delivery_time": spec["delivery_time"],
        "payment_method_id": spec["payment_method_id"],
        "payment_method": spec["payment_method"],
        "session_id": spec.get("session", ""),
        "total": _money(total),
        "payment_date": spec.get("payment_date", spec["date"] if is_paid else "0000-00-00"),
        "access_code": spec["access_code"],
        "shipment_integrator": spec["integrator"],
        "modified": spec["modified"],
        "printed": "",
        "interest": "0.00",
        "cart_additional_values_discount": "0.00",
        "cart_additional_values_increase": "0.00",
        "id_quotation": "",
        "estimated_delivery_date": spec["eta"],
        "is_traceable": "1" if spec.get("sending_code") else "",
        "external_code": spec.get("external_code", ""),
        "tracking_url": (f"https://rastreamento.correios.com.br/app/index.php?objeto={spec['sending_code']}"
                         if spec.get("sending_code") else ""),
        "has_payment": spec.get("has_payment", "1" if is_paid else "0"),
        "has_shipment": "1" if spec.get("sending_code") else "0",
        "has_invoice": spec["has_invoice"],
        "delivery_date": "",
        "dc_id": spec["dc_id"],
        "total_comission_user": "0.00",
        "total_comission": "0.00",
        "OrderStatus": {
            "id": status_id,
            "default": "1",
            "type": status_type,
            "show_backoffice": "1",
            "allow_edit_order": "0",
            "description": "",
            "status": status,
            "show_status_central": "",
            "background": background,
            "display_name": "",
            "font_color": "",
        },
        "PickupLocation": ([{
            "id": "3",
            "name": "Loja Rio Preto - SP",
            "address": "Praca Paul S. Harris",
            "number": "158",
            "neighborhood": "Centro",
            "city": "Sao Jose do Rio Preto",
            "state": "SP",
            "zip_code": "15010-011",
        }] if spec.get("pickup") else []),
        "cost": _money(cost_total),
        "app_id": "0",
        "urls": {
            "payment": (f"https://ecommerce.analisevirtual.com.br/loja/pagamento.php"
                        f"?loja={STORE_ID}&pedido={spec['access_code']}"),
        },
        "store_segment": "Eletronicos & Foto",
        "payment_method_type": spec["payment_type"],
        "Customer": build_customer(spec["customer"], spec),
        "ProductsSold": sold,
        "OrderInvoice": ([{"id": spec["id"], "link": ""}] if spec["has_invoice"] == "1" else []),
        "Payment": [],
        "MlOrder": [],
        "MarketplaceOrder": [],
        "OrderTransactions": [],
        "OrderInvoiceAmount": [],
        "OtherInvoiceAmounts": [],
        "ExtraTabs": [],
        "OrderChilds": [],
        "PaymentMethodMessage": {
            "text": "", "text_pag": "", "text_confirm": "", "confirmation": "0",
        },
        "payments_notification": {
            "notification": (f"https://{STORE_ID}.commercesuite.com.br/loja/retorno_pagamento.php"
                             f"?loja={STORE_ID}&gateway=5&codigoAcesso={spec['access_code']}"
                             f"&notification=true"),
        },
        "partner_name": "",
    }

    if coupon:
        order["coupon"] = {"code": coupon[0], "discount": coupon[1]}

    if spec.get("transaction_url"):
        order["OrderTransactions"] = [{"url_payment": spec["transaction_url"]}]

    payment_entry = spec.get("payment_entry")
    if payment_entry:
        order["Payment"] = [{"Payment": {
            "created": spec["modified"],
            "modified": spec["modified"],
            "id": payment_entry["id"],
            "order_id": spec["id"],
            "payment_method_id": "",
            "method": payment_entry["method"],
            "payment_place": "",
            "value": _money(total),
            "date": spec["date"],
            "note": "",
            "unique_number": payment_entry["unique_number"],
        }}]

    if spec["dc_id"] == "799":
        order["DistributionCenter"] = {
            "status": "completed",
            "type": "child_order",
            "group_id": "27927",
            "Package": {
                "id": "22861",
                "created_at": f"{spec['date']}T12:46:24.000000Z",
                "updated_at": f"{spec['date']}T12:57:46.000000Z",
                "order_id": spec["id"],
                "DistributionCenter": {
                    "id": "799",
                    "name": "Rio preto - SP",
                    "document": "",
                    "postal_code": "15010011",
                    "country": "Brasil",
                    "state": "SP",
                    "city": "Sao Jose do Rio Preto",
                    "address": "Praca Paul S. Harris",
                    "number": "158",
                    "neighborhood": "Centro",
                    "complement": "",
                    "updated_at": "2025-01-15T13:50:42.000000Z",
                    "created_at": "2025-01-15T13:47:57.000000Z",
                },
                "Products": [
                    {"product_id": pid, "variant_id": "0",
                     "product_sold_id": sold[i]["ProductsSold"]["id"], "quantity": str(qty)}
                    for i, (pid, qty) in enumerate(spec["items"])
                ],
            },
        }

    return order


AVAILABLE_FILTERS = [
    "id", "status", "partner_id", "session_id", "sending_code", "customer_id",
    "shipment", "point_sale", "payment_form", "access_code", "external_code",
    "has_payment", "has_invoice", "has_shipment", "with_statuses", "printed",
    "payment_method_id", "store_note", "discount_coupon", "dc_id", "modified",
    "date", "shipment_date", "payment_date",
]


def build_list_entry(order: dict) -> dict:
    """Versao resumida do pedido, como o endpoint de listagem retorna."""
    keep = [
        "status", "id", "date", "customer_id", "partial_total", "taxes", "discount",
        "point_sale", "shipment", "shipment_value", "shipment_date", "store_note",
        "discount_coupon", "payment_method_rate", "value_1", "sending_code",
        "session_id", "total", "payment_date", "access_code", "shipment_integrator",
        "modified", "printed", "interest", "id_quotation", "estimated_delivery_date",
        "external_code", "has_payment", "has_shipment", "has_invoice", "dc_id",
        "total_comission_user", "total_comission", "is_traceable",
    ]
    entry = {k: order[k] for k in keep if k in order}
    entry["payment_form"] = order["payment_method"]
    entry["progressive_discount"] = "0.00"
    entry["shipping_progressive_discount"] = "0.00"
    if "coupon" in order:
        entry["coupon"] = order["coupon"]
    entry["OrderStatus"] = order["OrderStatus"]
    entry["PickupLocation"] = order["PickupLocation"]
    # Na listagem a Bagy devolve apenas os IDs dos itens.
    entry["ProductsSold"] = [{"id": s["ProductsSold"]["id"]} for s in order["ProductsSold"]]
    entry["Payment"] = []
    entry["OrderInvoice"] = order["OrderInvoice"]
    entry["MlOrder"] = []
    entry["OrderTransactions"] = []
    entry["MarketplaceOrder"] = []
    entry["Extensions"] = []
    return {"Order": entry}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    orders = [build_order(spec) for spec in ORDER_SPECS]
    orders.sort(key=lambda o: int(o["id"]))

    for order in orders:
        payload = {"Order": order, "Extensions": [], "User": [], "Confirmation": []}
        path = OUT_DIR / f"order_{order['id']}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  detalhe  -> {path.name}")

    total = len(orders)
    pages = [orders[i:i + PAGE_LIMIT] for i in range(0, total, PAGE_LIMIT)]

    for index, chunk in enumerate(pages, start=1):
        payload = {
            "paging": {
                "total": total,
                "page": index,
                "offset": (index - 1) * PAGE_LIMIT,
                "limit": PAGE_LIMIT,
                "maxLimit": 50,
            },
            "sort": [{"id": "asc"}],
            "availableFilters": AVAILABLE_FILTERS,
            "appliedFilters": [],
            "Orders": [build_list_entry(o) for o in chunk],
        }
        path = OUT_DIR / f"orders_page_{index}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  listagem -> {path.name} ({len(chunk)} pedidos)")

    print(f"\n{total} pedidos em {len(pages)} paginas gerados em {OUT_DIR}")


if __name__ == "__main__":
    main()
