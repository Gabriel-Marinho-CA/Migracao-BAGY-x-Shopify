"""Funcoes utilitarias da transformacao."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from urllib.parse import urlparse

# Reaproveita o que ja foi testado na carga de pedidos com mocks.
from bagy2shopify.transform import (  # noqa: F401
    dec,
    e164_br,
    format_cnpj,
    format_cpf,
    is_valid_document,
    only_digits,
    province_code,
    slugify,
    split_name,
)

REF_PREFIX = "bagy-ref:"
# ID do objeto criado pela mutation principal do proprio payload.
SELF = "bagy-ref:self"
REF_RE = re.compile(r"bagy-ref:[a-z_]+:[^\s\"']+")
LOCAL_FORMAT = "%Y-%m-%d %H:%M:%S"
CDN_BASE = "https://cdn.dooca.store"


def ref(kind: str, key) -> str:
    return f"{REF_PREFIX}{kind}:{key}"


def collect_refs(*objects) -> list:
    return sorted(set(REF_RE.findall(json.dumps(objects, ensure_ascii=False))))


def money(value) -> str:
    return f"{dec(value):.2f}"


def money_bag(value, currency: str) -> dict:
    return {"shopMoney": {"amount": money(value), "currencyCode": currency}}


def parse_local(value) -> datetime | None:
    if not value or str(value).startswith("0000"):
        return None
    text = str(value).strip().replace("T", " ")
    if len(text) == 10:
        text += " 00:00:00"
    try:
        return datetime.strptime(text[:19], LOCAL_FORMAT)
    except ValueError:
        return None


def iso(value, offset: str) -> str | None:
    moment = parse_local(value)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + offset if moment else None


def iso_end_of_day(value, offset: str) -> str | None:
    moment = parse_local(value)
    return moment.strftime("%Y-%m-%dT23:59:59") + offset if moment else None


def valid_date(value) -> str | None:
    moment = parse_local(value)
    if not moment or moment.year < 1900:
        return None
    return moment.strftime("%Y-%m-%d")


def clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def truncate(value, limit: int) -> str | None:
    text = clean(value)
    return text[:limit] if text else None


def strip_html(value) -> str | None:
    text = clean(value)
    if not text:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text or None


def one_line(value) -> str | None:
    text = strip_html(value)
    return " ".join(text.split()) if text else None


def compact(mapping: dict) -> dict:
    """Remove chaves com None: a Shopify interpreta null como 'apagar o campo'."""
    return {key: value for key, value in mapping.items() if value is not None}


def zip_br(value) -> str | None:
    digits = only_digits(value or "")
    if len(digits) == 8:
        return f"{digits[:5]}-{digits[5:]}"
    return clean(value)


def resolve_media(url, shop_id) -> str | None:
    """`media://files/x.png` e o formato do editor visual da Bagy para o CDN."""
    text = clean(url)
    if not text:
        return None
    if text.startswith("media://"):
        return f"{CDN_BASE}/{shop_id}/{text[len('media://'):]}"
    if text.startswith("//"):
        return "https:" + text
    return text


def resolve_media_in_html(content, shop_id) -> str | None:
    text = clean(content)
    return text.replace("media://", f"{CDN_BASE}/{shop_id}/") if text else None


def image_src(value, shop_id) -> str | None:
    if isinstance(value, dict):
        value = value.get("src") or value.get("url")
    return resolve_media(value, shop_id) if isinstance(value, str) else None


def metafield(namespace: str, key: str, type_: str, value) -> dict | None:
    if value is None or value == "" or value == []:
        return None
    return {"namespace": namespace, "key": key, "type": type_,
            "value": value if isinstance(value, str) else str(value)}


def seo_metafields(title, description) -> list:
    """SEO de pagina e artigo: a Shopify usa os metafields global.title_tag/description_tag."""
    return [m for m in (
        metafield("global", "title_tag", "single_line_text_field", truncate(one_line(title), 255)),
        metafield("global", "description_tag", "single_line_text_field",
                  truncate(one_line(description), 320)),
    ) if m]


def tag_list(*values) -> list:
    tags = []
    for value in values:
        text = clean(value)
        if text and text[:255] not in tags:
            tags.append(text[:255])
    return tags


def store_path(url) -> str:
    # urlparse separa query e #ancora tambem em caminho relativo ou slug solto.
    path = urlparse(str(url or "")).path
    stripped = path.strip("/")
    return "/" + stripped if stripped else "/"


def mailing_address(address: dict | None, *, fallback_name=None, phone=None,
                    company=None) -> dict | None:
    """Endereco da Bagy -> MailingAddressInput (numero e complemento em linhas separadas)."""
    if not address:
        return None
    receiver = clean(address.get("receiver")) or clean(fallback_name)
    first_name, last_name = split_name(receiver or "")
    street, number = clean(address.get("street")), clean(address.get("number"))
    line1 = ", ".join(part for part in (street, number) if part)
    line2 = " - ".join(part for part in (clean(address.get("detail")),
                                         clean(address.get("district"))) if part)
    return compact({
        "firstName": first_name or None,
        "lastName": last_name or None,
        "address1": line1 or None,
        "address2": line2 or None,
        "city": clean(address.get("city")),
        "provinceCode": province_code(address.get("state") or ""),
        "zip": zip_br(address.get("zipcode")),
        "countryCode": "BR",
        "phone": phone,
        "company": clean(company),
    })


def rewrite_url(url, mapping: dict, hosts: set) -> tuple:
    """URL antiga -> caminho na Shopify. Devolve (url, resolvido); link externo passa intacto."""
    text = clean(url) or "/"
    parsed = urlparse(text)
    if parsed.netloc and parsed.netloc.lower() not in hosts:
        return text, True
    path = store_path(text)
    if path == "/":
        return "/", True
    target = mapping.get(path)
    return (target, True) if target else (path, False)


def closest_path(path: str, mapping: dict, cutoff: float = 0.85) -> str | None:
    """Caminho antigo quase igual a um que existe (ex.: produto renomeado de 44g para 45g)."""
    from difflib import get_close_matches
    matches = get_close_matches(path, list(mapping), n=1, cutoff=cutoff)
    return matches[0] if matches else None


HREF_RE = re.compile(r"""(href\s*=\s*["'])([^"']+)(["'])""", re.IGNORECASE)


def rewrite_links(content, mapping: dict, hosts: set) -> tuple:
    """Links internos do HTML apontando direto para a URL nova, sem salto de redirect.

    Mantem query string (utm) e ancora. Links externos, mailto e ancoras puras
    ficam intactos. Devolve (html, quantidade_reescrita).
    """
    text = clean(content)
    if not text or not mapping:
        return text, 0
    count = 0

    def replace(match):
        nonlocal count
        url = html.unescape(match.group(2))
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            return match.group(0)
        if parsed.netloc and parsed.netloc.lower() not in hosts:
            return match.group(0)
        if not parsed.netloc and not url.startswith("/"):
            return match.group(0)
        target = mapping.get(store_path(url))
        if not target:
            return match.group(0)
        new = target
        if parsed.query:
            new += ("&" if "?" in target else "?") + parsed.query
        if parsed.fragment:
            new += "#" + parsed.fragment
        count += 1
        return match.group(1) + html.escape(new, quote=True) + match.group(3)

    return HREF_RE.sub(replace, text), count
