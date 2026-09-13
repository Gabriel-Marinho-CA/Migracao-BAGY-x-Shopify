"""Descontos e cupons."""

from __future__ import annotations

from ..common import clean, compact, dec, iso, iso_end_of_day, money, ref, truncate
from ..model import Payload, skipped


def describe(discount: dict) -> str:
    parts = [f"tipo {discount.get('type')}"]
    value = dec(discount.get("value"))
    if value:
        unit = "%" if discount.get("value_type") == "percentage" else " reais"
        parts.append(f"{value:.0f}{unit}")
    if dec(discount.get("min_purchase")) > 0:
        parts.append(f"compra mínima R$ {money(discount['min_purchase'])}")
    if discount.get("zipcodes"):
        parts.append(f"{len(discount['zipcodes'])} faixa(s) de CEP")
    if discount.get("entitled_product_ids"):
        parts.append(f"{len(discount['entitled_product_ids'])} produto(s) específico(s)")
    if discount.get("date_from") or discount.get("date_to"):
        parts.append(f"vigência {discount.get('date_from') or '-'} a {discount.get('date_to') or 'sem fim'}")
    return f"desconto #{discount['id']} ({truncate(discount.get('name'), 60) or 'sem nome'}): " + ", ".join(parts)


def discounts(src, settings, reference) -> tuple:
    today = reference.strftime("%Y-%m-%d")
    offset = settings.timezone_offset
    products = {product["id"] for product in src.all("products")}
    categories = {category["id"] for category in src.all("categories")}
    seen_codes: dict = {}
    payloads, manual = [], []

    for discount in src.all("discounts"):
        key = f"discount:{discount['id']}"
        codes = [code["code"].strip() for code in discount.get("codes") or [] if clean(code.get("code"))]
        kind = discount.get("type")
        active = bool(discount.get("active"))
        date_to = clean(discount.get("date_to"))
        expired = bool(date_to and date_to < today)

        if not codes:
            if active and not expired:
                manual.append(describe(discount))
                payloads.append(skipped("discount", key,
                                        f"desconto automatico ({kind}) - configurar manualmente na Shopify"))
            else:
                payloads.append(skipped("discount", key, "desconto automatico inativo ou vencido"))
            continue
        if not active and not settings.include_inactive_discounts:
            payloads.append(skipped("discount", key, "inativo na Bagy"))
            continue
        if expired and not settings.include_inactive_discounts:
            payloads.append(skipped("discount", key, "vencido na Bagy"))
            continue
        if kind != "coupon":
            manual.append(describe(discount))
            payloads.append(skipped("discount", key, f"cupom do tipo {kind} - configurar manualmente"))
            continue

        code = codes[0]
        if code.upper() in seen_codes:
            payloads.append(skipped("discount", key,
                                    "codigo repetido (a Shopify nao diferencia maiusculas de minusculas)"))
            continue
        seen_codes[code.upper()] = key

        payload = Payload("discount", key, provides=[ref("discount", discount["id"])])
        if len(codes) > 1:
            payload.warn("desconto com mais de um codigo - so o primeiro foi migrado")

        entitled_products = discount.get("entitled_product_ids") or []
        entitled_categories = discount.get("entitled_category_ids") or []
        product_refs = [ref("product", pid) for pid in entitled_products if pid in products]
        category_refs = [ref("collection", f"category:{cid}") for cid in entitled_categories if cid in categories]
        if (entitled_products or entitled_categories) and not (product_refs or category_refs):
            # Sem os produtos, o cupom viraria desconto na loja toda.
            payloads.append(skipped("discount", key, "desconto restrito a produtos que nao existem mais"))
            continue
        if len(product_refs) < len(entitled_products) or len(category_refs) < len(entitled_categories):
            payload.warn("parte dos produtos ou categorias do desconto nao existe mais")
        if product_refs:
            items = {"products": {"productsToAdd": product_refs}}
        elif category_refs:
            items = {"collections": {"add": category_refs}}
        else:
            items = {"all": True}

        if discount.get("zipcodes"):
            payload.warn("restricao por faixa de CEP nao migrada")
        if discount.get("prerequisite_product_ids") or (discount.get("prerequisite_quantity") or 1) > 1:
            payload.warn("pre-requisito de produto ou quantidade nao migrado")

        base = compact({
            "title": truncate(discount.get("name"), 255) or code,
            "code": code,
            "startsAt": iso(discount.get("date_from") or discount.get("created_at"), offset),
            "endsAt": iso_end_of_day(date_to, offset),
            "context": {"all": "ALL"},
            "appliesOncePerCustomer": bool(discount.get("single_usage")),
            "usageLimit": discount.get("usage_limit"),
            "minimumRequirement": ({"subtotal": {"greaterThanOrEqualToSubtotal": money(discount["min_purchase"])}}
                                   if dec(discount.get("min_purchase")) > 0 else None),
            "combinesWith": {"orderDiscounts": False, "productDiscounts": False,
                             "shippingDiscounts": bool(discount.get("coupon_allow_free_freight"))},
        })

        value = dec(discount.get("value"))
        if value <= 0:
            if discount.get("is_free_freight"):
                payload.mutation = "discountCodeFreeShippingCreate"
                payload.variables = {"freeShippingCodeDiscount": dict(base, destination={"all": True})}
                payloads.append(payload)
            else:
                payloads.append(skipped("discount", key, "desconto com valor zero"))
            continue

        if discount.get("is_free_freight"):
            payload.warn("o cupom tambem dava frete gratis; na Shopify um codigo nao acumula os dois - "
                         "so o desconto foi migrado")
        if discount.get("value_type") == "percentage":
            if value > 100:
                payloads.append(skipped("discount", key, "percentual acima de 100%"))
                continue
            value_input = {"percentage": float(value / 100)}
        else:
            value_input = {"discountAmount": {"amount": money(value), "appliesOnEachItem": False}}

        payload.mutation = "discountCodeBasicCreate"
        payload.variables = {"basicCodeDiscount": dict(base, customerGets={"value": value_input,
                                                                            "items": items})}
        payloads.append(payload)

    return payloads, manual
