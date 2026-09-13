"""Pedidos: orderCreate sobre o formato real da API da Bagy (api.dooca.store).

Substitui o mapeamento do bagy2shopify, que foi escrito para os mocks no
formato antigo (Tray/CommerceSuite).
"""

from __future__ import annotations

from decimal import Decimal

from ..common import (
    SELF, clean, compact, dec, e164_br, format_cnpj, format_cpf, is_valid_document, iso,
    mailing_address, money, money_bag, only_digits, parse_local, ref, slugify, tag_list, zip_br,
)
from ..model import Payload

FINANCIAL = {
    "approved": "PAID",
    "pending": "PENDING",
    "refunded": "REFUNDED",
    "denied": "VOIDED",
    "expired": "EXPIRED",
}

STATUS_LABELS = {
    "approved": "pagamento aprovado",
    "attended": "em separação",
    "invoiced": "faturado (NF-e)",
    "shipped": "enviado",
    "delivered": "entregue",
    "canceled": "cancelado",
    "archived": "arquivado",
    "pending": "pendente",
}

# Nome da transportadora como a Shopify reconhece (gera link de rastreio).
CARRIERS = (
    ("loggi", "Loggi"),
    ("correios", "Correios"),
    ("l4b", "L4B Logística"),
    ("j3", "J3 Envios"),
    ("melhor envio", "Melhor Envio"),
    ("frenet", "Frenet"),
)

INVOICED_AS_SHIPPED = "NF-e emitida sem envio registrado e pedido antigo - migrado como enviado, sem rastreio"


def carrier_name(raw) -> str | None:
    text = clean(raw)
    if not text:
        return None
    lower = text.lower()
    for needle, name in CARRIERS:
        if needle in lower:
            return name
    return text


def history_note(order: dict) -> str:
    lines = [f"Pedido importado da Bagy (código {order.get('code')})."]
    events = sorted(order.get("histories") or [], key=lambda h: h.get("created_at") or "")
    if events:
        lines.append("Histórico na Bagy:")
    for event in events[:30]:
        label = STATUS_LABELS.get(event.get("status"), event.get("status"))
        who = f" ({event['user_type']})" if event.get("user_type") else ""
        note = f" — {clean(event['note'])}" if clean(event.get("note")) else ""
        lines.append(f"• {(event.get('created_at') or '')[:16]} {label}{who}{note}")
    return "\n".join(lines)


def custom_attributes(order: dict) -> list:
    """Card "Informacoes adicionais" do pedido, no padrao das integracoes brasileiras."""
    customer = order.get("customer") or {}
    payment = order.get("payment") or {}
    shipping = order.get("shipping") or {}
    fulfillment = order.get("fulfillment") or {}
    address = order.get("address") or {}
    utm = (order.get("extra") or {}).get("utm") or {}
    document = only_digits(customer.get("cgc") or "")
    is_cnpj = len(document) == 14
    order_bump = (any((item.get("source") or {}).get("slug") == "order-bump" for item in order.get("items") or [])
                  or any((source or {}).get("slug") == "order-bump" for source in order.get("source") or []))

    pairs = [
        ("info_document", document),
        ("info_document_type", "CNPJ" if is_cnpj else ("CPF" if document else None)),
        ("info_document_formatted", format_cnpj(document) if is_cnpj else (format_cpf(document) if document else None)),
        ("info_document_valid", "false" if document and not is_valid_document(document) else None),
        ("info_ie", clean(customer.get("ie"))),
        ("info_company", clean(customer.get("company"))),
        ("customer_phone", clean(customer.get("phone"))),
        ("bagy_order_id", order.get("id")),
        ("bagy_order_code", order.get("code")),
        ("bagy_order_status", order.get("status")),
        ("bagy_payment_status", order.get("payment_status")),
        ("bagy_fulfillment_status", order.get("fulfillment_status")),
        ("payment_method", payment.get("method")),
        ("payment_gateway", payment.get("gateway_name")),
        ("payment_installments", payment.get("parcels")),
        ("payment_card_brand", payment.get("card_brand")),
        ("payment_method_discount", money(payment.get("discount")) if dec(payment.get("discount")) > 0 else None),
        ("shipping_method", shipping.get("alias")),
        ("shipping_service", shipping.get("name")),
        ("shipping_api", shipping.get("api")),
        ("shipping_delivery_days", shipping.get("delivery_time")),
        ("shipping_estimated_delivery", (shipping.get("estimated_delivery_at") or "")[:10] or None),
        ("tracking_code", fulfillment.get("shipping_code")),
        ("shipping_carrier", fulfillment.get("shipping_carrier")),
        ("tracking_url", fulfillment.get("shipping_track_url")),
        ("nfe_number", fulfillment.get("nfe_number")),
        ("nfe_series", fulfillment.get("nfe_series")),
        ("nfe_key", fulfillment.get("nfe_token")),
        ("utm_source", utm.get("utm_source")),
        ("utm_medium", utm.get("utm_medium")),
        ("utm_campaign", utm.get("utm_campaign")),
        ("device", order.get("device")),
        ("order_bump", "true" if order_bump else None),
        ("shipping_street_name", address.get("street")),
        ("shipping_street_number", address.get("number")),
        ("shipping_street_complement", address.get("detail")),
        ("shipping_neighborhood", address.get("district")),
        ("shipping_city", address.get("city")),
        ("shipping_province", address.get("state")),
        ("shipping_zip", zip_br(address.get("zipcode"))),
        ("shipping_city_ibge", address.get("city_ibge_id")),
    ]
    return [{"key": key, "value": str(value)[:1000]}
            for key, value in pairs if value not in (None, "", False)]


def discount_code(order: dict, amount: Decimal, currency: str, payload: Payload) -> dict:
    """A Shopify aceita um codigo por pedido: cupom + cashback + desconto do Pix viram um so."""
    parts = []
    for entry in order.get("discounts") or []:
        if entry.get("type") == "cashback":
            parts.append("CASHBACK")
        elif clean(entry.get("code")):
            parts.append(entry["code"].strip().upper())
        else:
            parts.append((slugify(entry.get("name") or "") or "desconto").upper()[:30])
    payment = order.get("payment") or {}
    if dec(payment.get("discount")) > 0:
        parts.append("PIX" if payment.get("method") == "pix" else "PAGAMENTO")
    parts = list(dict.fromkeys(parts)) or ["DESCONTO-BAGY"]
    if len(parts) > 1:
        payload.warn("mais de um desconto somado num unico codigo (a Shopify aceita um por pedido)")
    return {"itemFixedDiscountCode": {"code": "+".join(parts)[:255], "amountSet": money_bag(amount, currency)}}


def orders(src, settings, reference) -> list:
    variations = {variation["id"] for variation in src.all("variations")}
    customers = {customer["id"] for customer in src.all("customers")}
    return [transform_order(order, settings, reference, variations, customers)
            for order in src.all("orders")]


def transform_order(order: dict, settings, reference, variations: set, customers: set) -> Payload:
    currency, offset = settings.currency, settings.timezone_offset
    payload = Payload("order", f"order:{order['id']}", "orderCreate", provides=[ref("order", order["id"])])
    customer = order.get("customer") or {}
    payment = order.get("payment") or {}
    shipping = order.get("shipping") or {}
    fulfillment_data = order.get("fulfillment") or {}
    items = order.get("items") or []
    status = order.get("status")
    payment_status = order.get("payment_status")
    fulfillment_status = order.get("fulfillment_status")

    # --- valores ---------------------------------------------------------
    # O desconto e derivado do total: o `discount` da Bagy arredonda o desconto
    # do Pix de outro jeito e fica 1 centavo acima em parte dos pedidos.
    subtotal = dec(order.get("subtotal"))
    shipping_price = dec(shipping.get("price"))
    tax = dec(order.get("tax"))
    total = dec(order.get("total"))
    discount = subtotal + shipping_price + tax - total
    if discount < 0:
        payload.warn("total maior que subtotal + frete - acrescimo sem campo equivalente na Shopify")
        discount = Decimal("0.00")
    elif discount != dec(order.get("discount")):
        payload.warn("desconto ajustado pelo arredondamento da Bagy para o total bater")

    line_items = []
    for item in items:
        variation_id = item.get("variation_id")
        weight = dec(item.get("weight"))
        linked = variation_id in variations
        if not linked:
            payload.warn("item sem variacao no catalogo - criado como item avulso")
        line_items.append(compact({
            "title": clean(item.get("name")) or "Produto",
            "quantity": int(item.get("quantity") or 1),
            "priceSet": money_bag(item.get("price"), currency),
            "sku": clean(item.get("sku")),
            "variantId": ref("variant", variation_id) if linked else None,
            "requiresShipping": not item.get("is_virtual"),
            "taxable": False,
            "weight": {"value": float(weight), "unit": "KILOGRAMS"} if weight > 0 else None,
            "properties": [{"name": "Brinde", "value": "Sim"}] if item.get("is_gift") else None,
        }))

    items_total = sum((dec(item.get("price")) * int(item.get("quantity") or 1) for item in items),
                      Decimal("0.00"))
    computed = items_total + shipping_price + tax - discount
    payload.meta = {"total_bagy": money(total), "total_shopify": money(computed)}
    if money(computed) != money(total):
        payload.warn("total calculado para a Shopify difere do total da Bagy")

    # --- pagamento -------------------------------------------------------
    financial = FINANCIAL.get(payment_status)
    if financial is None:
        payload.warn("status de pagamento desconhecido - migrado como pendente")
        financial = "PENDING"
    paid_at = iso(payment.get("processed_at"), offset) or iso(order.get("created_at"), offset)
    authorization = clean((payment.get("extra") or {}).get("authorization_code"))
    transactions = []
    if payment_status in ("approved", "refunded"):
        transactions.append(compact({
            "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
            "amountSet": money_bag(total, currency), "processedAt": paid_at,
            "authorizationCode": authorization,
        }))
    if payment_status == "refunded":
        transactions.append(compact({
            "kind": "REFUND", "status": "SUCCESS", "gateway": "manual",
            "amountSet": money_bag(total, currency),
            "processedAt": iso(payment.get("updated_at"), offset) or paid_at,
        }))

    # --- envio -----------------------------------------------------------
    created = parse_local(order.get("created_at"))
    age_days = (reference - created).days if created else 0
    fulfilled = status != "canceled" and (
        fulfillment_status in ("shipped", "delivered")
        or (fulfillment_status in ("invoiced", "attended") and payment_status == "approved"
            and age_days > settings.fulfill_invoiced_after_days))
    fulfillment = None
    if fulfilled:
        tracking = clean(fulfillment_data.get("shipping_code"))
        fulfillment = compact({
            "locationId": ref("location", "default"),
            "notifyCustomer": False,
            "trackingNumber": tracking,
            "trackingCompany": (carrier_name(fulfillment_data.get("shipping_carrier") or shipping.get("alias"))
                                if tracking else None),
            "shipmentStatus": "DELIVERED" if fulfillment_status == "delivered" else None,
        })
        if fulfillment_status in ("invoiced", "attended"):
            payload.warn(INVOICED_AS_SHIPPED)
        elif not tracking:
            payload.warn("pedido enviado sem codigo de rastreio")

    closed_at = None
    if status == "archived":
        archived = next((h for h in order.get("histories") or [] if h.get("status") == "archived"), None)
        closed_at = iso((archived or {}).get("created_at") or order.get("updated_at"), offset)

    # --- cliente e endereco ----------------------------------------------
    customer_id = order.get("customer_id")
    if customer_id not in customers:
        payload.warn("cliente do pedido nao existe na extracao - pedido criado sem cliente")
    phone = e164_br(customer.get("phone") or "")
    is_company = customer.get("entity") == "company"
    address = mailing_address(order.get("address"), fallback_name=customer.get("name"), phone=phone,
                              company=customer.get("company") if is_company else None)
    if not address:
        payload.warn("pedido sem endereco")
    email = clean(customer.get("email"))

    order_input = compact({
        "name": f"#{order['code']}" if settings.order_name_from_code and order.get("code") else None,
        "currency": currency,
        "processedAt": iso(order.get("created_at"), offset),
        "closedAt": closed_at,
        "email": email.lower() if email else None,
        "phone": phone,
        "customer": {"toAssociate": {"id": ref("customer", customer_id)}} if customer_id in customers else None,
        "shippingAddress": address,
        "billingAddress": address,
        "lineItems": line_items,
        "shippingLines": [compact({
            "title": clean(shipping.get("alias")) or clean(shipping.get("name")) or "Frete",
            "priceSet": money_bag(shipping_price, currency),
            "code": str(shipping["carrier_id"]) if shipping.get("carrier_id") else None,
            "source": clean(shipping.get("api")),
        })] if shipping else None,
        "discountCode": discount_code(order, discount, currency, payload) if discount > 0 else None,
        "financialStatus": financial,
        "transactions": transactions or None,
        "fulfillment": fulfillment,
        "note": history_note(order),
        "tags": tag_list("bagy-import", f"bagy-id-{order['id']}", f"bagy-status-{status}",
                         f"bagy-pagamento-{payment_status}", f"bagy-envio-{fulfillment_status}"),
        "customAttributes": custom_attributes(order),
        "sourceIdentifier": f"bagy-{order['id']}",
        "test": False,
    })
    payload.variables = {"order": order_input,
                         "options": {"inventoryBehaviour": "BYPASS", "sendReceipt": False,
                                     "sendFulfillmentReceipt": False}}

    # --- pos-acoes -------------------------------------------------------
    document = only_digits(customer.get("cgc") or "")
    if document and is_valid_document(document):
        # localizedFields nao existe no orderCreate; a Shopify valida o documento.
        payload.post_actions.append({"mutation": "orderUpdate", "variables": {"input": {
            "id": SELF, "localizedFields": [{"key": "TAX_CREDENTIAL_BR", "value": document}]}}})
    elif document:
        payload.warn("CPF/CNPJ invalido - fica so nas informacoes adicionais do pedido")
    if fulfilled:
        # O `fulfillment` do orderCreate fecha um so fulfillment order.
        payload.post_actions.append({"action": "fulfillment_sweep", "notifyCustomer": False})
    if status == "canceled":
        payload.post_actions.append({"mutation": "orderCancel", "variables": {
            "orderId": SELF,
            "reason": "DECLINED" if payment_status in ("denied", "expired") else "OTHER",
            "restock": False,
            "notifyCustomer": False,
            "staffNote": f"Cancelado na Bagy em {str(order.get('canceled_at') or order.get('updated_at') or '')[:16]}",
        }})
    return payload
