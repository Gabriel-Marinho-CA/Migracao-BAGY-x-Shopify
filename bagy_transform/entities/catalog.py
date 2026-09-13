"""Catalogo: definicoes de metafield, colecoes (categorias) e produtos."""

from __future__ import annotations

import json
import re

from ..common import (
    clean, compact, dec, image_src, metafield, money, one_line, only_digits, ref,
    rewrite_links, slugify, strip_html, truncate,
)
from ..model import Payload, publish_action, skipped

PRODUCT_FIELDS = (
    ("descricao_curta", "Descrição curta", "multi_line_text_field",
     "Descrição curta do produto na Bagy"),
    ("conteudo_pagina", "Conteúdo extra", "page_reference",
     "Página com o conteúdo rico que acompanhava o produto na Bagy (hotsite)"),
)

CUSTOMER_FIELDS = (
    ("cpf_cnpj", "CPF/CNPJ", "single_line_text_field",
     "Documento fiscal do cliente, importado da Bagy"),
    ("data_nascimento", "Data de nascimento", "date", "Data de nascimento informada na Bagy"),
    ("genero", "Gênero", "single_line_text_field", "Gênero informado na Bagy"),
    ("inscricao_estadual", "Inscrição estadual", "single_line_text_field",
     "Inscrição estadual (pessoa jurídica)"),
    ("telefone", "Telefone (Bagy)", "single_line_text_field",
     "Telefone original da Bagy. Guardado aqui porque a Shopify exige telefone único no cadastro"),
)


def feature_key(feature: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", slugify(feature.get("slug") or feature.get("name") or ""))
    return ("caracteristica_" + base.strip("_"))[:64]


def definition_ref(owner: str, key: str) -> str:
    return ref("metafield_definition", f"{owner.lower()}.custom.{key}")


def metafield_definitions(src) -> list:
    payloads = []

    def add(owner: str, key: str, name: str, type_: str, description: str) -> None:
        source_key = f"{owner.lower()}.custom.{key}"
        payloads.append(Payload(
            "metafield_definition", source_key, "metafieldDefinitionCreate",
            {"definition": {"ownerType": owner, "namespace": "custom", "key": key,
                            "name": name, "description": description, "type": type_,
                            "pin": True}},
            provides=[ref("metafield_definition", source_key)]))

    for feature in src.all("features"):
        add("PRODUCT", feature_key(feature), truncate(feature.get("name"), 255) or feature_key(feature),
            "list.single_line_text_field", "Característica do produto na Bagy")
    for key, name, type_, description in PRODUCT_FIELDS:
        add("PRODUCT", key, name, type_, description)
    for key, name, type_, description in CUSTOMER_FIELDS:
        add("CUSTOMER", key, name, type_, description)
    return payloads


def collections(src) -> list:
    shop_id = src.shop_id
    payloads = []
    for category in src.all("categories"):
        key = f"category:{category['id']}"
        image = image_src(category.get("image"), shop_id)
        collection = compact({
            "title": category["name"],
            "handle": category["slug"],
            "descriptionHtml": clean(category.get("description")),
            "seo": compact({
                "title": truncate(one_line(category.get("meta_title")), 255),
                "description": truncate(one_line(category.get("meta_description")), 320),
            }) or None,
            "sortOrder": "BEST_SELLING",
            "image": compact({"src": image, "altText": category["name"]}) if image else None,
            "metafields": [metafield("bagy", "category_id", "number_integer", category["id"])],
        })
        # `collection: CollectionCreateInput`: o argumento `input` esta deprecado.
        payload = Payload("collection", key, "collectionCreate", {"collection": collection},
                          provides=[ref("collection", key)])
        payload.post_actions.append(publish_action())
        if not category.get("active", True):
            payload.warn("categoria inativa na Bagy")
        payloads.append(payload)
    return payloads


def products(src, mapping: dict | None = None) -> list:
    shop_id = src.shop_id
    features = {feature["id"]: feature for feature in src.all("features")}
    active_hotsites = {hotsite["id"] for hotsite in src.all("hotsites") if hotsite.get("active")}
    payloads = []

    for product in src.all("products"):
        key = f"product:{product['id']}"
        variations = product.get("variations") or []
        if len(variations) != 1:
            payloads.append(skipped(
                "product", key,
                "produto com mais de uma variacao - a transformacao atual cobre so produto simples"))
            continue
        variation = variations[0]
        payload = Payload("product", key, "productSet",
                          provides=[ref("product", product["id"]), ref("variant", variation["id"])])

        price = (variation.get("price") if variation.get("price") not in (None, "")
                 else product.get("price"))
        compare = variation.get("price_compare") or product.get("price_compare")
        product_active = bool(product.get("active"))
        variation_active = bool(variation.get("active", True))
        if product_active != variation_active:
            payload.warn("produto e variacao com status diferentes na Bagy - migrado como rascunho")

        ncm = only_digits(product.get("ncm") or "")
        weight = dec(product.get("weight"))
        inventory_item = compact({
            "tracked": True,
            "requiresShipping": not product.get("is_virtual"),
            # Peso da Bagy em kg: 0.230 no produto de 225 g.
            "measurement": {"weight": {"value": float(weight), "unit": "KILOGRAMS"}} if weight > 0 else None,
            "harmonizedSystemCode": ncm[:6] if len(ncm) >= 6 else None,
            "countryHarmonizedSystemCodes": ([{"countryCode": "BR", "harmonizedSystemCode": ncm}]
                                             if len(ncm) == 8 else None),
        })
        variant = compact({
            # Produto sem opcoes: convencao da Shopify para a variante padrao.
            "optionValues": [{"optionName": "Title", "name": "Default Title"}],
            "price": money(price),
            "compareAtPrice": money(compare) if compare and dec(compare) > dec(price) else None,
            "sku": clean(variation.get("sku")),
            "barcode": clean(variation.get("gtin")),
            "inventoryPolicy": "CONTINUE" if product.get("sell_out_of_stock") else "DENY",
            "taxable": False,
            "inventoryItem": inventory_item,
            "inventoryQuantities": [{"locationId": ref("location", "default"), "name": "available",
                                     "quantity": int(variation.get("balance") or 0)}],
            "metafields": [metafield("bagy", "variation_id", "number_integer", variation["id"])],
        })

        files = []
        for image in sorted(product.get("images") or [], key=lambda i: i.get("position") or 0):
            src_url = image_src(image, shop_id)
            if src_url:
                files.append({"originalSource": src_url,
                              "alt": clean(image.get("alt")) or product["name"],
                              "contentType": "IMAGE"})
        if not files:
            payload.warn("produto sem imagens")

        metafields = []
        for feature in product.get("features") or []:
            values = [value["name"] for value in
                      sorted(feature.get("values") or [], key=lambda v: v.get("position") or 0)
                      if clean(value.get("name"))]
            if values:
                field_key = feature_key(features.get(feature.get("id"), feature))
                metafields.append(metafield("custom", field_key, "list.single_line_text_field",
                                            json.dumps(values, ensure_ascii=False)))
                payload.extra_depends.append(definition_ref("PRODUCT", field_key))

        short_description = strip_html(product.get("short_description"))
        if short_description:
            metafields.append(metafield("custom", "descricao_curta", "multi_line_text_field",
                                        short_description))
            payload.extra_depends.append(definition_ref("PRODUCT", "descricao_curta"))

        hotsite_id = product.get("hotsite_id")
        if hotsite_id and hotsite_id in active_hotsites:
            metafields.append(metafield("custom", "conteudo_pagina", "page_reference",
                                        ref("page", f"hotsite:{hotsite_id}")))
            payload.extra_depends.append(definition_ref("PRODUCT", "conteudo_pagina"))
        elif hotsite_id:
            payload.warn("hotsite ligado ao produto esta inativo - vinculo nao migrado")

        metafields.append(metafield("bagy", "product_id", "number_integer", product["id"]))

        description = clean(product.get("description"))
        if description and mapping:
            description, rewritten = rewrite_links(description, mapping, src.store_hosts)
            if rewritten:
                payload.meta["links_rewritten"] = rewritten

        product_input = compact({
            "title": product["name"],
            "handle": product["slug"],
            "descriptionHtml": description,
            "vendor": clean((product.get("brand") or {}).get("name")),
            "productType": clean((product.get("category_default") or {}).get("name")),
            "status": "ACTIVE" if product_active and variation_active else "DRAFT",
            "seo": compact({
                "title": truncate(one_line(product.get("meta_title")), 255),
                "description": truncate(one_line(product.get("meta_description")), 320),
            }) or None,
            "tags": ["bagy-import"],
            "collections": [ref("collection", f"category:{c['id']}")
                            for c in product.get("categories") or []] or None,
            "files": files or None,
            "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
            "variants": [variant],
            "metafields": [m for m in metafields if m],
        })
        payload.variables = {"identifier": {"handle": product["slug"]},
                             "input": product_input, "synchronous": True}
        payload.post_actions.append(publish_action())
        payloads.append(payload)
    return payloads
