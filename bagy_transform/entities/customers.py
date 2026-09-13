"""Clientes, leads da newsletter e credito de cashback."""

from __future__ import annotations

import hashlib
from collections import Counter

from ..common import (
    SELF, clean, compact, dec, e164_br, iso, is_valid_document, mailing_address, metafield,
    money, only_digits, ref, split_name, tag_list, valid_date,
)
from ..model import Payload, skipped
from .catalog import definition_ref

GENDER = {"female": "Feminino", "male": "Masculino"}


def consent_action(created_at, offset: str) -> dict:
    """customerSet nao aceita consentimento de marketing: vai numa chamada propria."""
    return {"mutation": "customerEmailMarketingConsentUpdate",
            "variables": {"input": {"customerId": SELF, "emailMarketingConsent": compact({
                "marketingState": "SUBSCRIBED",
                "marketingOptInLevel": "SINGLE_OPT_IN",
                "consentUpdatedAt": iso(created_at, offset),
            })}}}


def metafields_action(payload: Payload, fields: list) -> None:
    """customerSet tambem nao aceita metafields: metafieldsSet logo depois."""
    items = [dict(field, ownerId=SELF) for field in fields if field]
    if not items:
        return
    payload.post_actions.append({"mutation": "metafieldsSet", "variables": {"metafields": items}})
    payload.extra_depends.extend(definition_ref("CUSTOMER", item["key"])
                                 for item in items if item["namespace"] == "custom")


def customers(src, settings) -> tuple:
    offset = settings.timezone_offset
    addresses = {address["customer_id"]: address for address in src.all("customer_addresses")}
    used_phones: set = set()
    payloads = []

    # Ordem por ID: quem cadastrou primeiro fica com o telefone quando ha repeticao.
    for customer in sorted(src.all("customers"), key=lambda c: c["id"]):
        key = f"customer:{customer['id']}"
        email = (customer.get("email") or "").strip().lower()
        if not email:
            payloads.append(skipped("customer", key, "cliente sem e-mail"))
            continue
        payload = Payload("customer", key, "customerSet", provides=[ref("customer", customer["id"])])

        phone = e164_br(customer.get("phone") or "")
        phone_digits = only_digits(customer.get("phone") or "")
        profile_phone = None
        if phone and phone not in used_phones:
            profile_phone = phone
            used_phones.add(phone)
        elif phone:
            payload.warn("telefone ja usado por outro cliente (a Shopify exige telefone unico) - "
                         "mantido no metafield custom.telefone")
        elif phone_digits:
            payload.warn("telefone em formato invalido - mantido no metafield custom.telefone")

        is_company = customer.get("entity") == "company"
        address = mailing_address(addresses.get(customer["id"]) or customer.get("address"),
                                  fallback_name=customer.get("name"), phone=phone,
                                  company=customer.get("company") if is_company else None)
        if not address:
            payload.warn("cliente sem endereco na Bagy")

        document = only_digits(customer.get("cgc") or "")
        if document and not is_valid_document(document):
            payload.warn("CPF/CNPJ nao passa na validacao - gravado mesmo assim no metafield")

        payload.variables = {"identifier": {"email": email}, "input": compact({
            "email": email,
            "firstName": clean(customer.get("first_name")),
            "lastName": clean(customer.get("last_name")),
            "phone": profile_phone,
            "locale": "pt-BR",
            "note": (f"Cliente importado da Bagy (ID {customer['id']}), "
                     f"cadastrado em {str(customer.get('created_at') or '')[:10]}."),
            "tags": tag_list("bagy-import", "pessoa-juridica" if is_company else None,
                             "newsletter" if customer.get("newsletter") else None),
            "addresses": [address] if address else None,
        })}

        metafields_action(payload, [
            metafield("custom", "cpf_cnpj", "single_line_text_field", document or None),
            metafield("custom", "data_nascimento", "date", valid_date(customer.get("birthday"))),
            metafield("custom", "genero", "single_line_text_field", GENDER.get(customer.get("gender"))),
            metafield("custom", "inscricao_estadual", "single_line_text_field", clean(customer.get("ie"))),
            metafield("custom", "telefone", "single_line_text_field", phone_digits or None),
            metafield("bagy", "customer_id", "number_integer", customer["id"]),
        ])
        if customer.get("newsletter"):
            payload.post_actions.append(consent_action(customer.get("created_at"), offset))
        payloads.append(payload)

    return payloads, used_phones


def leads(src, settings, used_phones: set) -> list:
    """Inscritos na newsletter que nunca se cadastraram como cliente."""
    if not settings.include_leads:
        return []
    offset = settings.timezone_offset
    customer_emails = {(c.get("email") or "").strip().lower() for c in src.all("customers")}
    payloads = []

    for entry in src.all("mailings_all"):
        email = (entry.get("email") or "").strip().lower()
        if not email or entry.get("is_customer") or email in customer_emails:
            continue
        # Chave sem o e-mail em claro: aparece em relatorio e log.
        digest = hashlib.sha1(email.encode("utf-8")).hexdigest()[:16]
        payload = Payload("lead", f"lead:{digest}", "customerSet", provides=[ref("lead", digest)])

        first_name, last_name = split_name(clean(entry.get("name")) or "")
        phone = e164_br(entry.get("phone") or "")
        profile_phone = None
        if phone and phone not in used_phones:
            profile_phone = phone
            used_phones.add(phone)
        elif phone:
            payload.warn("telefone ja usado por outro cadastro - mantido no metafield custom.telefone")

        payload.variables = {"identifier": {"email": email}, "input": compact({
            "email": email,
            "firstName": first_name or None,
            "lastName": last_name or None,
            "phone": profile_phone,
            "locale": "pt-BR",
            "note": (f"Inscrito na newsletter da Bagy em {str(entry.get('created_at') or '')[:10]}, "
                     "sem cadastro de cliente."),
            "tags": ["bagy-import", "lead-newsletter"],
        })}
        metafields_action(payload, [
            metafield("custom", "data_nascimento", "date", valid_date(entry.get("birthday"))),
            metafield("custom", "genero", "single_line_text_field", GENDER.get(entry.get("gender"))),
            metafield("custom", "telefone", "single_line_text_field",
                      only_digits(entry.get("phone") or "") or None),
        ])
        payload.post_actions.append(consent_action(entry.get("created_at"), offset))
        payloads.append(payload)
    return payloads


def store_credits(src, settings, reference) -> tuple:
    """Cashback disponivel vira credito da loja, preservando a data de expiracao.

    Semantica confirmada nos dados: `used` nos creditos soma o mesmo que os
    debitos aprovados, entao o saldo de cada credito e value - used.
    """
    now = reference.strftime("%Y-%m-%d %H:%M:%S")
    customer_ids = {customer["id"] for customer in src.all("customers")}
    payloads, stats = [], Counter()

    for entry in src.all("cashback_entries"):
        if entry.get("type") != "credit" or entry.get("status") != "approved":
            stats["debito, cancelado ou pendente"] += 1
            continue
        if entry.get("expires_at") and entry["expires_at"] <= now:
            stats["credito expirado"] += 1
            continue
        remaining = dec(entry.get("value")) - dec(entry.get("used"))
        if remaining <= 0:
            stats["credito ja usado"] += 1
            continue
        key = f"cashback:{entry['id']}"
        if entry.get("release_at") and entry["release_at"] > now:
            stats["credito ainda nao liberado"] += 1
            payloads.append(skipped("store_credit", key,
                                    "credito so e liberado no futuro - refazer extracao e "
                                    "transformacao depois da liberacao"))
            continue
        if entry.get("customer_id") not in customer_ids:
            stats["cliente inexistente"] += 1
            payloads.append(skipped("store_credit", key, "cliente do credito nao existe na extracao"))
            continue
        stats["credito migrado"] += 1
        payloads.append(Payload(
            "store_credit", key, "storeCreditAccountCredit",
            {"id": ref("customer", entry["customer_id"]),
             "creditInput": compact({
                 "creditAmount": {"amount": money(remaining), "currencyCode": settings.currency},
                 "expiresAt": iso(entry.get("expires_at"), settings.timezone_offset),
                 "notify": False,
             })},
            meta={"amount": money(remaining)}))
    return payloads, stats
