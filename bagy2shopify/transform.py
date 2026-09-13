"""Converte um pedido da Bagy no input do `orderCreate` da Shopify.

Funcoes puras: nao tocam rede nem disco, entao dao para rodar e conferir com
`--dry-run` antes de qualquer credencial existir.

Decisoes de mapeamento (todas documentadas no README):

* Itens viram *custom line items* (titulo + preco), sem `variantId`. Os escopos
  liberados nao incluem produtos, e o catalogo nao foi migrado.
* Estoque nao e decrementado (`inventoryBehaviour: BYPASS`).
* CPF/RG/telefone e as observacoes da Bagy vao para o campo `note` do pedido.
* A Shopify aceita **um unico** desconto por pedido, entao cupom + desconto de
  pagamento (ex.: abatimento do Pix) sao somados num unico codigo fixo.
* `payment_method_rate` positivo (juros de parcelamento) vira uma linha de
  acrescimo, porque a Shopify nao tem campo de sobretaxa no `orderCreate`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal("0.01")

# Estados brasileiros aceitos como provinceCode pela Shopify.
UF = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
}


@dataclass(frozen=True)
class StatusMapping:
    financial: str          # OrderCreateFinancialStatus
    fulfilled: bool         # cria fulfillment junto com o pedido?
    paid: bool              # gera transacao SALE/SUCCESS?


# Bagy -> Shopify. Chaves normalizadas (sem acento, maiusculas).
STATUS_MAP = {
    "A ENVIAR": StatusMapping("PAID", False, True),
    "EM SEPARACAO": StatusMapping("PAID", False, True),
    "PRONTO PARA RETIRADA": StatusMapping("PAID", False, True),
    "ENVIADO": StatusMapping("PAID", True, True),
    "ENTREGUE": StatusMapping("PAID", True, True),
    "FINALIZADO": StatusMapping("PAID", True, True),
    "AGUARDANDO PAGAMENTO": StatusMapping("PENDING", False, False),
    "PAGAMENTO EM ANALISE": StatusMapping("PENDING", False, False),
    "EM ANALISE": StatusMapping("PENDING", False, False),
    "CANCELADO": StatusMapping("VOIDED", False, False),
    "ESTORNADO": StatusMapping("REFUNDED", False, False),
    "DEVOLVIDO": StatusMapping("REFUNDED", False, False),
}

DEFAULT_STATUS = StatusMapping("PENDING", False, False)


@dataclass
class TransformResult:
    bagy_id: str
    order_input: dict
    options: dict
    warnings: list = field(default_factory=list)
    # `orderCreate` nao aceita localizedFields; vao num orderUpdate logo depois.
    localized_fields: list = field(default_factory=list)
    # Se o pedido deve terminar despachado (exige varredura pos-criacao).
    should_fulfill: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize_status(value: str) -> str:
    return strip_accents(value or "").strip().upper()


def slugify(value: str) -> str:
    value = strip_accents(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "sem-valor"


def dec(value, default: str = "0.00") -> Decimal:
    """Converte os valores em string da Bagy para Decimal, tolerando vazios."""
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value.quantize(CENTS, rounding=ROUND_HALF_UP)
    text = str(value).strip().replace(" ", "")
    if not text:
        return Decimal(default)
    # A Bagy usa ponto decimal, mas notas em pt-BR podem vir com virgula.
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text).quantize(CENTS, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal(default)


def money_bag(amount: Decimal, currency: str) -> dict:
    return {"shopMoney": {"amount": f"{amount:.2f}", "currencyCode": currency}}


def is_blank_date(value: str) -> bool:
    return not value or value.startswith("0000-00-00")


def iso_datetime(date: str, hour: str, offset: str) -> str | None:
    """'2025-02-06' + '09:46:09' -> '2025-02-06T09:46:09-03:00'."""
    if is_blank_date(date):
        return None
    time_part = (hour or "").strip() or "00:00:00"
    if len(time_part) == 5:
        time_part += ":00"
    return f"{date}T{time_part}{offset}"


def format_cpf(cpf: str) -> str:
    digits = re.sub(r"\D", "", cpf or "")
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return cpf or ""


def format_cnpj(cnpj: str) -> str:
    digits = re.sub(r"\D", "", cnpj or "")
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return cnpj or ""


# DDDs brasileiros validos (Anatel).
DDDS = {11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
        41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69,
        71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97,
        98, 99}


def e164_br(number: str) -> str | None:
    """Telefone brasileiro em E.164 (+55DDDNUMERO), so se for um numero valido.

    A Shopify valida o telefone de verdade ("Phone is invalid"). Aqui:
    * celular antigo, sem o 9 (DDD + 8 digitos comecando com 6-9): ganha o 9,
      como na migracao das operadoras em 2016;
    * fixo (DDD + 8 digitos comecando com 2-5) fica como esta;
    * DDD inexistente, ou celular de 11 digitos que nao comeca com 9: None
      (nao da para adivinhar - o numero original fica guardado no metafield).
    """
    digits = re.sub(r"\D", "", number or "").lstrip("0")
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if len(digits) not in (10, 11) or int(digits[:2]) not in DDDS:
        return None
    ddd, local = digits[:2], digits[2:]
    if len(local) == 9:
        return f"+55{ddd}{local}" if local[0] == "9" else None
    if local[0] in "6789":
        return f"+55{ddd}9{local}"
    if local[0] in "2345":
        return f"+55{ddd}{local}"
    return None


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def province_code(state: str) -> str | None:
    code = strip_accents(state or "").strip().upper()
    return code if code in UF else None


# --------------------------------------------------------------------------- #
# Blocos do pedido
# --------------------------------------------------------------------------- #

def build_line_items(order: dict, currency: str, warnings: list) -> tuple[list, Decimal]:
    line_items = []
    subtotal = Decimal("0.00")

    for wrapper in order.get("ProductsSold") or []:
        item = wrapper.get("ProductsSold", wrapper)

        if "price" not in item:
            # Acontece na resposta de listagem, que traz so o ID do item.
            warnings.append(
                f"item {item.get('id')} veio sem preco (payload de listagem, nao de detalhe) - ignorado"
            )
            continue

        quantity = int(dec(item.get("quantity"), "1"))
        if quantity <= 0:
            warnings.append(f"item {item.get('id')} com quantidade {quantity} - ignorado")
            continue

        price = dec(item.get("price"))
        subtotal += price * quantity

        title = (item.get("original_name") or item.get("name") or "Produto").strip()
        sku = (item.get("reference") or "").strip()

        line: dict = {
            "title": title,
            "quantity": quantity,
            "priceSet": money_bag(price, currency),
            "requiresShipping": str(item.get("virtual_product", "0")) != "1",
            # No Brasil o imposto ja vem embutido no preco de venda.
            "taxable": False,
        }
        if sku:
            line["sku"] = sku
        if (item.get("brand") or "").strip():
            line["vendor"] = item["brand"].strip()

        properties = []
        if item.get("product_id"):
            properties.append({"name": "bagy_product_id", "value": str(item["product_id"])})
        variant_id = str(item.get("variant_id") or "0")
        if variant_id not in ("", "0"):
            properties.append({"name": "bagy_variant_id", "value": variant_id})
        if (item.get("text_variant") or "").strip():
            properties.append({"name": "variacao", "value": item["text_variant"].strip()})
        if properties:
            line["properties"] = properties

        weight = dec(item.get("weight"), "0")
        if weight > 0:
            line["weight"] = {"value": float(weight), "unit": "GRAMS"}

        line_items.append(line)

    return line_items, subtotal


def build_addresses(order: dict, warnings: list) -> tuple[dict | None, dict | None]:
    customer = order.get("Customer") or {}
    if not customer:
        warnings.append("pedido sem bloco Customer - endereco nao preenchido")
        return None, None

    addresses = customer.get("CustomerAddresses") or []
    source = addresses[0].get("CustomerAddress", addresses[0]) if addresses else customer

    recipient = (source.get("recipient") or customer.get("name") or "").strip()
    first_name, last_name = split_name(recipient or customer.get("name", ""))

    street = (source.get("address") or "").strip()
    number = (source.get("number") or "").strip()
    address1 = f"{street}, {number}".strip(", ") if number else street

    address2_parts = [p for p in (
        (source.get("complement") or "").strip(),
        (source.get("neighborhood") or "").strip(),
    ) if p]

    address: dict = {
        "firstName": first_name or "Cliente",
        "lastName": last_name or "Bagy",
        "address1": address1 or "Nao informado",
        "city": (source.get("city") or "").strip() or "Nao informado",
        "zip": (source.get("zip_code") or "").strip(),
        "countryCode": "BR",
    }
    if address2_parts:
        address["address2"] = " - ".join(address2_parts)

    code = province_code(source.get("state", ""))
    if code:
        address["provinceCode"] = code
    else:
        warnings.append(f"UF invalida ou ausente: {source.get('state')!r}")

    phone = e164_br(customer.get("cellphone") or "") or e164_br(customer.get("phone") or "")
    if phone:
        address["phone"] = phone

    return address, dict(address)


def build_customer_input(order: dict, warnings: list) -> tuple[dict | None, str | None]:
    customer = order.get("Customer") or {}
    email = (customer.get("email") or "").strip().lower()

    if not email:
        bagy_customer_id = order.get("customer_id")
        warnings.append(
            f"cliente {bagy_customer_id} sem e-mail - pedido sera criado sem cliente associado"
        )
        return None, None

    first_name, last_name = split_name(customer.get("name", ""))
    upsert: dict = {"email": email}
    if first_name:
        upsert["firstName"] = first_name
    if last_name:
        upsert["lastName"] = last_name

    notes = []
    if customer.get("cpf"):
        notes.append(f"CPF: {format_cpf(customer['cpf'])}")
    if customer.get("cnpj"):
        notes.append(f"CNPJ: {format_cnpj(customer['cnpj'])}")
    if customer.get("rg"):
        notes.append(f"RG: {customer['rg']}")
    if customer.get("id"):
        notes.append(f"ID Bagy: {customer['id']}")
    if notes:
        upsert["note"] = " | ".join(notes)

    tags = ["bagy-import"]
    if customer.get("id"):
        tags.append(f"bagy-customer-{customer['id']}")
    upsert["tags"] = tags

    return {"toUpsert": upsert}, email


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def is_valid_cpf(value: str) -> bool:
    """Valida CPF pelos digitos verificadores.

    A Shopify valida de verdade o TAX_CREDENTIAL_BR: um CPF errado faz o
    `orderUpdate` inteiro falhar. Filtrar aqui evita perder tambem os
    customAttributes que iriam na mesma chamada.
    """
    digits = only_digits(value)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False

    numbers = [int(d) for d in digits]
    for size in (9, 10):
        total = sum(numbers[i] * (size + 1 - i) for i in range(size))
        remainder = total % 11
        expected = 0 if remainder < 2 else 11 - remainder
        if numbers[size] != expected:
            return False
    return True


def is_valid_cnpj(value: str) -> bool:
    digits = only_digits(value)
    if len(digits) != 14 or len(set(digits)) == 1:
        return False

    numbers = [int(d) for d in digits]
    for weights in ([5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2],
                    [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]):
        size = len(weights)
        total = sum(numbers[i] * weights[i] for i in range(size))
        remainder = total % 11
        expected = 0 if remainder < 2 else 11 - remainder
        if numbers[size] != expected:
            return False
    return True


# CPFs com digitos validos que a Shopify recusa mesmo assim, por serem
# sequencias obviamente falsas.
BLOCKED_DOCUMENTS = {"12345678909", "01234567890"}


def is_valid_document(value: str) -> bool:
    digits = only_digits(value)
    if digits in BLOCKED_DOCUMENTS:
        return False
    if len(digits) == 11:
        return is_valid_cpf(digits)
    if len(digits) == 14:
        return is_valid_cnpj(digits)
    return False


def build_note(order: dict) -> str:
    """Apenas as observacoes em texto livre da Bagy.

    O resto (documentos, pagamento, frete, endereco) vai para
    `customAttributes`, que no admin aparece como "Informacoes adicionais" em
    pares chave/valor - legivel e pesquisavel, ao contrario de texto solto.
    """
    lines = []

    customer_note = (order.get("customer_note") or "").strip()
    if customer_note:
        lines.append(f"Obs. do cliente: {customer_note}")

    store_note = (order.get("store_note") or "").strip()
    if store_note:
        parts = [p.strip() for p in store_note.splitlines() if p.strip()]
        lines.append(f"Obs. da loja: {' / '.join(parts)}")

    return "\n".join(lines)


def build_localized_fields(order: dict, warnings: list | None = None) -> list:
    """CPF/CNPJ no campo localizado do Brasil.

    E o que o admin mostra como "Informacoes adicionais -> CPF/CNPJ" dentro do
    card do Cliente. Nao da para mandar no `orderCreate` (o input nao tem
    `localizedFields`), entao vai numa chamada `orderUpdate` logo depois.

    Documento invalido nao e enviado: a Shopify recusa e derrubaria a chamada
    inteira. O valor bruto continua nos customAttributes, marcado como invalido,
    para nao se perder.
    """
    customer = order.get("Customer") or {}
    # CNPJ tem precedencia: se existe, o cliente e pessoa juridica.
    document = only_digits(customer.get("cnpj") or "") or only_digits(customer.get("cpf") or "")
    if not document:
        return []

    if not is_valid_document(document):
        if warnings is not None:
            warnings.append(
                f"documento {document!r} nao passa na validacao de CPF/CNPJ - nao "
                "enviado ao campo do admin (fica em info_document como invalido)"
            )
        return []

    return [{"key": "TAX_CREDENTIAL_BR", "value": document}]


def build_custom_attributes(order: dict) -> list:
    """Dados estruturados para o card "Informacoes adicionais" do pedido.

    Chaves em snake_case, no mesmo padrao que as integracoes brasileiras de
    Shopify usam (`info_document`, `shipping_street_name`, ...).
    """
    customer = order.get("Customer") or {}
    addresses = customer.get("CustomerAddresses") or []
    address = (addresses[0].get("CustomerAddress", addresses[0]) if addresses else customer)

    cnpj = only_digits(customer.get("cnpj") or "")
    cpf = only_digits(customer.get("cpf") or "")
    document = cnpj or cpf

    rate = dec(order.get("payment_method_rate"))
    pickup = (order.get("PickupLocation") or [])

    street = (address.get("address") or "").strip()
    number = (address.get("number") or "").strip()
    complement = (address.get("complement") or "").strip()
    neighborhood = (address.get("neighborhood") or "").strip()
    city = (address.get("city") or "").strip()
    state = (address.get("state") or "").strip()
    zip_code = (address.get("zip_code") or "").strip()

    full_address = ", ".join(p for p in (street, number) if p)
    if complement:
        full_address += f" {complement}"
    tail = " - ".join(p for p in (neighborhood, f"{city}/{state}" if city else "") if p)
    if tail:
        full_address += f" - {tail}"
    if zip_code:
        full_address += f" - {only_digits(zip_code)}"

    pairs = [
        # Documentos
        ("info_document", document),
        ("info_document_type", "CNPJ" if cnpj else ("CPF" if cpf else "")),
        ("info_document_formatted", format_cnpj(cnpj) if cnpj else format_cpf(cpf)),
        # Marcado so quando invalido, para o dado nao se perder em silencio.
        ("info_document_valid", "" if (not document or is_valid_document(document))
                                else "false"),
        ("info_rg", (customer.get("rg") or "").strip()),
        ("info_state_inscription", (customer.get("state_inscription") or "").strip()),
        ("info_company_name", (customer.get("company_name") or "").strip()),

        # Contato
        ("customer_phone", (customer.get("phone") or "").strip()),
        ("customer_cellphone", (customer.get("cellphone") or "").strip()),

        # Origem na Bagy
        ("bagy_order_id", str(order.get("id") or "")),
        ("bagy_order_status", (order.get("status") or "").strip()),
        ("bagy_customer_id", str(order.get("customer_id") or "")),
        ("bagy_access_code", (order.get("access_code") or "").strip()),
        ("bagy_external_code", (order.get("external_code") or "").strip()),
        ("sales_channel", (order.get("point_sale") or "").strip()),

        # Pagamento
        ("payment_method", (order.get("payment_method") or "").strip()),
        ("payment_type", (order.get("payment_method_type") or "").strip()),
        ("payment_installments", (order.get("installment") or "").strip()),
        ("payment_rate", f"{rate:.2f}" if rate else ""),
        ("payment_date", "" if is_blank_date(order.get("payment_date", ""))
                         else order.get("payment_date", "")),

        # Frete
        ("shipping_method", (order.get("shipment") or "").strip()),
        ("shipping_carrier", (order.get("shipment_integrator") or "").strip()),
        ("shipping_estimated_delivery",
         "" if is_blank_date(order.get("estimated_delivery_date", ""))
         else order.get("estimated_delivery_date", "")),
        ("shipping_tracking_code", (order.get("sending_code") or "").strip()),

        # Endereco destrinchado
        ("shipping_street_name", street),
        ("shipping_street_number", number),
        ("shipping_street_complement", complement),
        ("shipping_neighborhood", neighborhood),
        ("shipping_city", city),
        ("shipping_province", state),
        ("shipping_zip", zip_code),
        ("shipping_full_address", full_address),
        ("billing_same_as_shipping", "true"),
    ]

    if pickup:
        place = pickup[0]
        pairs.append(("pickup_location", (place.get("name") or "").strip()))
        pairs.append((
            "pickup_address",
            f"{place.get('address', '')}, {place.get('number', '')} - "
            f"{place.get('city', '')}/{place.get('state', '')}".strip(" ,-"),
        ))

    # key e value sao String! - descarta o que estiver vazio.
    return [{"key": key, "value": value} for key, value in pairs if value]


def build_tags(order: dict) -> list:
    tags = ["bagy-import", f"bagy-id-{order.get('id')}"]
    if order.get("status"):
        tags.append(f"bagy-status-{slugify(order['status'])}")
    if order.get("payment_method"):
        tags.append(f"bagy-pagamento-{slugify(order['payment_method'])}")
    if order.get("point_sale"):
        tags.append(f"bagy-canal-{slugify(order['point_sale'])}")
    if (order.get("PickupLocation") or []):
        tags.append("bagy-retirada-na-loja")
    return tags


def build_discount(order: dict, currency: str, warnings: list) -> tuple[dict | None, Decimal]:
    """Consolida cupom + descontos num unico codigo (limite da Shopify)."""
    coupon = order.get("coupon") or {}
    coupon_value = dec(coupon.get("discount"))
    order_discount = dec(order.get("discount"))
    rate = dec(order.get("payment_method_rate"))
    payment_discount = -rate if rate < 0 else Decimal("0.00")

    total = coupon_value + order_discount + payment_discount
    if total <= 0:
        return None, Decimal("0.00")

    code = (coupon.get("code") or "").strip().upper()
    if not code:
        code = "DESCONTO-BAGY"

    components = sum(1 for v in (coupon_value, order_discount, payment_discount) if v > 0)
    if components > 1:
        warnings.append(
            f"a Shopify aceita so um desconto por pedido: cupom + demais descontos "
            f"foram somados em R$ {total:.2f} sob o codigo {code}"
        )

    return {"itemFixedDiscountCode": {"code": code, "amountSet": money_bag(total, currency)}}, total


def build_shipping_lines(order: dict, currency: str) -> list:
    title = (order.get("shipment") or "").strip()
    value = dec(order.get("shipment_value"))
    if not title and value == 0:
        return []

    line: dict = {
        "title": title or "Frete",
        "priceSet": money_bag(value, currency),
    }
    integrator = (order.get("shipment_integrator") or "").strip()
    if integrator:
        line["source"] = integrator
    return [line]


def build_fulfillment(order: dict, location_id: str | None, warnings: list) -> dict | None:
    if not location_id:
        warnings.append(
            "pedido esta finalizado/entregue mas nenhum locationId foi informado - "
            "criado sem fulfillment"
        )
        return None

    fulfillment: dict = {"locationId": location_id, "notifyCustomer": False}
    tracking_number = (order.get("sending_code") or "").strip()
    if tracking_number:
        fulfillment["trackingNumber"] = tracking_number
        integrator = (order.get("shipment_integrator") or "").strip()
        if integrator:
            fulfillment["trackingCompany"] = integrator
    if str(order.get("delivered") or "") == "1":
        fulfillment["shipmentStatus"] = "DELIVERED"
    return fulfillment


# --------------------------------------------------------------------------- #
# Entrada principal
# --------------------------------------------------------------------------- #

def to_shopify_order(
    order: dict,
    *,
    currency: str = "BRL",
    timezone_offset: str = "-03:00",
    location_id: str | None = None,
    source_name: str | None = None,
) -> TransformResult:
    """Traduz um pedido completo da Bagy para `OrderCreateOrderInput`."""
    warnings: list = []
    bagy_id = str(order.get("id", ""))

    mapping = STATUS_MAP.get(normalize_status(order.get("status", "")))
    if mapping is None:
        mapping = DEFAULT_STATUS
        warnings.append(
            f"status {order.get('status')!r} nao mapeado - usando "
            f"{DEFAULT_STATUS.financial} e sem fulfillment"
        )

    line_items, subtotal = build_line_items(order, currency, warnings)
    if not line_items:
        warnings.append("pedido sem itens utilizaveis - nao pode ser criado na Shopify")

    discount, discount_total = build_discount(order, currency, warnings)
    shipping_lines = build_shipping_lines(order, currency)
    shipping_value = dec(order.get("shipment_value"))

    # Juros de parcelamento nao tem campo proprio no orderCreate: vira linha.
    rate = dec(order.get("payment_method_rate"))
    interest = dec(order.get("interest"))
    surcharge = (rate if rate > 0 else Decimal("0.00")) + interest
    if surcharge > 0:
        line_items.append({
            "title": f"Acrescimo - {order.get('payment_method') or 'meio de pagamento'}",
            "quantity": 1,
            "priceSet": money_bag(surcharge, currency),
            "requiresShipping": False,
            "taxable": False,
            "properties": [{"name": "origem", "value": "payment_method_rate (Bagy)"}],
        })
        warnings.append(
            f"juros/taxa de R$ {surcharge:.2f} adicionado como item de acrescimo "
            "(a Shopify nao tem campo de sobretaxa no orderCreate)"
        )

    computed_total = subtotal + surcharge - discount_total + shipping_value
    bagy_total = dec(order.get("total"))
    if bagy_total and abs(computed_total - bagy_total) > CENTS:
        warnings.append(
            f"divergencia de total: Bagy R$ {bagy_total:.2f} x calculado "
            f"R$ {computed_total:.2f} (diferenca R$ {computed_total - bagy_total:.2f})"
        )

    shipping_address, billing_address = build_addresses(order, warnings)
    customer_input, email = build_customer_input(order, warnings)

    processed_at = iso_datetime(order.get("date", ""), order.get("hour", ""), timezone_offset)

    order_input: dict = {
        "currency": currency,
        "financialStatus": mapping.financial,
        "lineItems": line_items,
        "customAttributes": build_custom_attributes(order),
        "tags": build_tags(order),
        "sourceIdentifier": f"bagy-{bagy_id}",
        "test": False,
    }

    note = build_note(order)
    if note:
        order_input["note"] = note

    if processed_at:
        order_input["processedAt"] = processed_at
    if email:
        order_input["email"] = email
    if customer_input:
        order_input["customer"] = customer_input
    if shipping_address:
        order_input["shippingAddress"] = shipping_address
    if billing_address:
        order_input["billingAddress"] = billing_address
    if shipping_lines:
        order_input["shippingLines"] = shipping_lines
    if discount:
        order_input["discountCode"] = discount
    if source_name:
        order_input["sourceName"] = source_name

    phone = None
    customer = order.get("Customer") or {}
    if customer:
        phone = e164_br(customer.get("cellphone") or "") or e164_br(customer.get("phone") or "")
    if phone:
        order_input["phone"] = phone

    if mapping.paid and bagy_total > 0:
        paid_at = order.get("payment_date")
        transaction: dict = {
            "kind": "SALE",
            "status": "SUCCESS",
            # Os gateways da Bagy (Yapay, Vindi) nao existem na Shopify; o
            # registro entra como pagamento manual preservando o nome original.
            "gateway": "manual",
            "amountSet": money_bag(bagy_total, currency),
            "test": False,
        }
        if not is_blank_date(paid_at or ""):
            transaction["processedAt"] = iso_datetime(paid_at, "00:00:00", timezone_offset)
        elif processed_at:
            transaction["processedAt"] = processed_at
        order_input["transactions"] = [transaction]

    if mapping.fulfilled:
        fulfillment = build_fulfillment(order, location_id, warnings)
        if fulfillment:
            order_input["fulfillment"] = fulfillment

    if normalize_status(order.get("status", "")) == "CANCELADO":
        warnings.append(
            "pedido CANCELADO na Bagy: o orderCreate nao cria pedido cancelado. "
            "Criado como VOIDED e marcado com a tag bagy-status-cancelado; "
            "para cancelar de fato rode `orderCancel` depois."
        )

    options = {
        "inventoryBehaviour": "BYPASS",   # migracao historica nao mexe em estoque
        "sendReceipt": False,
        "sendFulfillmentReceipt": False,
    }

    localized_fields = build_localized_fields(order, warnings)

    return TransformResult(
        bagy_id=bagy_id,
        order_input=order_input,
        options=options,
        warnings=warnings,
        localized_fields=localized_fields,
        should_fulfill=mapping.fulfilled,
    )
