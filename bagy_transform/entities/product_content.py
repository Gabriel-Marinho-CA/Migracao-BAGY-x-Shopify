"""Conteudo rico dos produtos (hotsites da Bagy) -> metaobjetos + metafields.

Na Bagy o produto aponta para um hotsite montado no editor visual. Aqui so o
que tem estrutura vira dado da Shopify, pensado para o tema fazer loop em Liquid:

* tabela nutricional -> metaobjeto `tabela_nutricional`; cada linha e texto no
                        padrao "Nutriente | valor | valor | %VD"
* especificacoes     -> metaobjeto `especificacoes` com listas: titulos[i] + conteudos[i]
* depoimentos        -> metaobjeto `depoimentos` com listas: clientes[i], textos[i],
                        notas[i], fotos[i]
* selos              -> metafield `list.file_reference` (so as imagens)
* modo de uso e ingredientes -> metafields rich text

As secoes livres (imagem + texto, banners, carrossel) ficam para o tema. Os
hotsites que nao sao de produto continuam como paginas (content.py).

Um metaobjeto por produto; conteudo identico entre produtos (ex.: guarana 100g
e 250g) vira uma entrada so, pelo handle: `metaobjectUpsert` nao duplica.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from urllib.parse import urlparse

from bagy2shopify.transform import strip_accents

from ..common import compact, image_src, metafield, ref, slugify, strip_html, truncate
from ..model import Payload, skipped
from ..richtext import html_to_rich_text, nutrition_table

IMAGE_ONLY = (("file_type_options", '["Image"]'),)
RATING_SCALE = (("scale_min", "1.0"), ("scale_max", "5.0"))

# (type, nome, descricao, campo de exibicao, campos)
# campo: (key, nome, tipo, obrigatorio, validacoes)
METAOBJECT_DEFINITIONS = (
    ("tabela_nutricional", "Tabela nutricional",
     "Informação nutricional. Cada linha: Nutriente | valor | valor | %VD (as colunas seguem o campo Colunas).",
     "nome", (
         ("nome", "Nome", "single_line_text_field", True, ()),
         ("porcao", "Porção", "single_line_text_field", False, ()),
         ("porcoes_por_embalagem", "Porções por embalagem", "single_line_text_field", False, ()),
         ("colunas", "Colunas (cabeçalho dos valores)", "list.single_line_text_field", False, ()),
         ("linhas", "Linhas (Nutriente | valor | valor | %VD)", "list.single_line_text_field", True, ()),
         ("observacao", "Observação", "multi_line_text_field", False, ()),
     )),
    ("especificacoes", "Especificações",
     "Especificações do produto em listas paralelas: títulos[i] com conteúdos[i].",
     "nome", (
         ("nome", "Nome", "single_line_text_field", True, ()),
         ("titulos", "Títulos", "list.single_line_text_field", True, ()),
         ("conteudos", "Conteúdos", "list.single_line_text_field", True, ()),
     )),
    ("depoimentos", "Depoimentos",
     "Depoimentos em listas paralelas: clientes[i], textos[i], notas[i] e fotos[i].",
     "nome", (
         ("nome", "Nome", "single_line_text_field", True, ()),
         ("clientes", "Clientes", "list.single_line_text_field", True, ()),
         ("textos", "Textos", "list.single_line_text_field", True, ()),
         ("notas", "Notas (1 a 5)", "list.rating", False, RATING_SCALE),
         ("fotos", "Fotos", "list.file_reference", False, IMAGE_ONLY),
     )),
)

# Metafields do produto: (key, nome, tipo, validacoes, descricao). Referencia a
# metaobjeto valida pelo ID da definicao (a Shopify recusa criar so com o tipo:
# "Validations require that you select a metaobject"). Recriar a definicao do
# metaobjeto exige recriar estes campos: run_load.py --cleanup faz em cascata.
PRODUCT_CONTENT_FIELDS = (
    ("tabela_nutricional", "Tabela nutricional", "metaobject_reference",
     (("metaobject_definition_id", "bagy-ref:metaobject_definition:tabela_nutricional"),),
     "Informação nutricional (metaobjeto)"),
    ("modo_de_uso", "Modo de uso", "rich_text_field", (), "Como consumir ou usar o produto"),
    ("ingredientes", "Ingredientes", "rich_text_field", (), "Ingredientes do produto"),
    ("selos", "Selos", "list.file_reference", IMAGE_ONLY, "Imagens dos selos e certificações"),
    ("especificacoes", "Especificações", "metaobject_reference",
     (("metaobject_definition_id", "bagy-ref:metaobject_definition:especificacoes"),),
     "Títulos e conteúdos: ingrediente, peso, origem... (metaobjeto)"),
    ("depoimentos", "Depoimentos", "metaobject_reference",
     (("metaobject_definition_id", "bagy-ref:metaobject_definition:depoimentos"),),
     "Depoimentos de clientes (metaobjeto)"),
)

PLACEHOLDERS = {"insira um titulo", "insira um subtitulo", "botao de acao"}
NUTRITION = re.compile(r"nutri")
USAGE = re.compile(r"consum|incluir|modo de uso|como usar")
INGREDIENTS = re.compile(r"ingredient")


def definition_ref(type_: str) -> str:
    return ref("metaobject_definition", type_)


def metaobject_definitions() -> list:
    payloads = []
    for type_, name, description, display, fields in METAOBJECT_DEFINITIONS:
        payloads.append(Payload(
            "metaobject_definition", f"metaobject_definition:{type_}", "metaobjectDefinitionCreate",
            {"definition": {
                "type": type_, "name": name, "description": description, "displayNameKey": display,
                "access": {"storefront": "PUBLIC_READ"},
                "fieldDefinitions": [compact({
                    "key": key, "name": field_name, "type": field_type, "required": required,
                    "validations": [{"name": v_name, "value": v_value} for v_name, v_value in validations] or None,
                }) for key, field_name, field_type, required, validations in fields],
            }},
            provides=[definition_ref(type_)]))
    return payloads


def _plain(value) -> str:
    return " ".join((strip_html(value) or "").split()) if value else ""


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(_plain(value)).lower()).strip()


def _placeholder(*values) -> bool:
    return any(_norm(value) in PLACEHOLDERS for value in values if value)


def product_hotsites(src) -> dict:
    """hotsite_id -> produtos que usam o hotsite (ativos primeiro)."""
    linked = defaultdict(list)
    for product in sorted(src.all("products"), key=lambda p: (not p.get("active"), p["id"])):
        if product.get("hotsite_id"):
            linked[product["hotsite_id"]].append(product)
    return linked


def sections(hotsite: dict) -> list:
    content = hotsite.get("content") or {}
    found = content.get("sections") or {}
    order = [key for key in content.get("content_for_index") or [] if key in found]
    order += [key for key in found if key not in order]
    excluded = {key for key in content.get("sections_exclude") or [] if isinstance(key, str)}
    return [found[key] or {} for key in order if key not in excluded]


def extract(hotsite: dict, shop_id) -> dict:
    """O que o hotsite tem de estruturado. A primeira ocorrencia de cada campo vence."""
    found = {"tabela": None, "modo_de_uso": None, "ingredientes": None,
             "selos": [], "especificacoes": [], "depoimentos": [], "livres": Counter()}
    for section in sections(hotsite):
        kind = section.get("type")
        blocks = [(block or {}).get("settings") or {} for block in section.get("blocks") or []]
        used = False
        if kind == "image-text":
            for block in blocks:
                title, text = _norm(block.get("title")), block.get("text")
                if not _plain(text) or _placeholder(block.get("title"), text):
                    continue
                if NUTRITION.search(title) and "<table" in str(text).lower():
                    if found["tabela"] is None:
                        found["tabela"] = text
                    used = True
                elif USAGE.search(title):
                    found["modo_de_uso"] = found["modo_de_uso"] or text
                    used = True
                elif INGREDIENTS.search(title):
                    found["ingredientes"] = found["ingredientes"] or text
                    used = True
        elif kind == "banners-commercial":
            for block in blocks:
                title, description = _plain(block.get("title")), _plain(block.get("description"))
                image = image_src(block.get("image"), shop_id)
                if title and description and not _placeholder(title, description):
                    found["especificacoes"].append((title, description))
                    used = True
                elif image and not title:
                    found["selos"].append(image)
                    used = True
        elif kind == "reviews":
            for block in blocks:
                client, text = _plain(block.get("client")), _plain(block.get("description"))
                if client and text and not _placeholder(client, text):
                    found["depoimentos"].append({"cliente": client, "texto": text,
                                                 "nota": block.get("star_rating"),
                                                 "foto": image_src(block.get("image"), shop_id)})
                    used = True
        if not used:
            found["livres"][kind] += 1
    return found


def _score(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build(src, settings) -> dict:
    shop_id = src.shop_id
    hotsites = {hotsite["id"]: hotsite for hotsite in src.all("hotsites")}
    files, metaobjects, contents = {}, {}, []
    shared = {}

    def file_ref(url: str, alt: str | None) -> str:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        if key not in files:
            files[key] = Payload("file", f"file:{key}", "fileCreate", {"files": [compact({
                "originalSource": url, "alt": truncate(alt, 512), "contentType": "IMAGE"})]},
                provides=[ref("file", key)],
                meta={"filename": PurePosixPath(urlparse(url).path).name.lower()})
        return ref("file", key)

    def entry(type_: str, handle: str, name: str, fields: dict, warnings: list) -> str:
        """Uma entrada por conteudo: produtos com conteudo identico apontam para a mesma."""
        values = {key: value for key, value in fields.items() if value not in (None, "", "[]")}
        digest = hashlib.sha1(json.dumps([type_, values], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if digest in shared:
            warnings.append(f"{type_} identico ao de outro produto - usa a mesma entrada")
            return shared[digest]
        handle = slugify(handle)[:100].strip("-") or digest[:10]
        key = f"{type_}:{handle}"
        metaobjects[key] = Payload(
            "metaobject", f"metaobject:{key}", "metaobjectUpsert",
            {"handle": {"type": type_, "handle": handle},
             "metaobject": {"fields": [{"key": "nome", "value": truncate(name, 255)}]
                            + [{"key": k, "value": v} for k, v in values.items()]}},
            provides=[ref("metaobject", key)], extra_depends=[definition_ref(type_)])
        shared[digest] = ref("metaobject", key)
        return shared[digest]

    for hotsite_id, products in product_hotsites(src).items():
        hotsite = hotsites.get(hotsite_id)
        if not hotsite or not hotsite.get("active"):
            continue
        found = extract(hotsite, shop_id)
        warnings, values = [], []
        slug, product_name = products[0]["slug"], products[0]["name"]

        table = nutrition_table(found["tabela"]) if found["tabela"] else None
        if table:
            lines = [" | ".join([row["nutriente"], *(value or "-" for value in row["valores"])])
                     for row in table["linhas"]]
            reference = entry("tabela_nutricional", f"tabela-{slug}", f"Tabela nutricional - {product_name}", {
                "porcao": truncate(table["porcao"], 255),
                "porcoes_por_embalagem": truncate(table["porcoes_por_embalagem"], 255),
                "colunas": json.dumps(table["colunas"], ensure_ascii=False) if table["colunas"] else None,
                "linhas": json.dumps(lines, ensure_ascii=False),
                "observacao": table["observacao"],
            }, warnings)
            values.append(metafield("custom", "tabela_nutricional", "metaobject_reference", reference))

        for key, source in (("modo_de_uso", found["modo_de_uso"]), ("ingredientes", found["ingredientes"])):
            rich = html_to_rich_text(source)
            if rich:
                values.append(metafield("custom", key, "rich_text_field", rich))

        seals = list(dict.fromkeys(file_ref(url, PurePosixPath(urlparse(url).path).stem) for url in found["selos"]))
        if seals:
            values.append(metafield("custom", "selos", "list.file_reference", json.dumps(seals)))

        if found["especificacoes"]:
            reference = entry("especificacoes", f"especificacoes-{slug}", f"Especificações - {product_name}", {
                "titulos": json.dumps([truncate(t, 255) for t, _ in found["especificacoes"]], ensure_ascii=False),
                "conteudos": json.dumps([truncate(c, 255) for _, c in found["especificacoes"]], ensure_ascii=False),
            }, warnings)
            values.append(metafield("custom", "especificacoes", "metaobject_reference", reference))

        reviews = found["depoimentos"]
        if reviews:
            scores = [_score(review["nota"]) for review in reviews]
            if any(score and score > 5 for score in scores):
                warnings.append("nota de depoimento acima de 5 na Bagy - gravada como 5 (a loja exibia 5 estrelas)")
            scores = [min(score, 5) if score else None for score in scores]
            # Listas paralelas: notas e fotos so entram se todos os depoimentos tiverem.
            notes = (json.dumps([{"value": f"{score}.0", "scale_min": "1.0", "scale_max": "5.0"} for score in scores])
                     if all(score and score >= 1 for score in scores) else None)
            photos = (json.dumps([file_ref(review["foto"], review["cliente"]) for review in reviews])
                      if all(review["foto"] for review in reviews) else None)
            if notes is None and any(scores):
                warnings.append("nem todo depoimento tem nota - notas nao migradas (listas paralelas)")
            if photos is None and any(review["foto"] for review in reviews):
                warnings.append("nem todo depoimento tem foto - fotos nao migradas (listas paralelas)")
            reference = entry("depoimentos", f"depoimentos-{slug}", f"Depoimentos - {product_name}", {
                "clientes": json.dumps([truncate(r["cliente"], 255) for r in reviews], ensure_ascii=False),
                "textos": json.dumps([r["texto"] for r in reviews], ensure_ascii=False),
                "notas": notes,
                "fotos": photos,
            }, warnings)
            values.append(metafield("custom", "depoimentos", "metaobject_reference", reference))

        free = sum(found["livres"].values())
        if free:
            warnings.append(f"{free} secao(oes) livres do hotsite (imagem + texto, banners...) ficam para o tema")
        for product in products:
            key = f"product_content:{product['id']}"
            if not values:
                contents.append(skipped("product_content", key,
                                        "hotsite sem tabela, modo de uso, ingredientes, selos, especificacoes "
                                        "ou depoimentos", warnings))
                continue
            payload = Payload("product_content", key, "metafieldsSet",
                              {"metafields": [{"ownerId": ref("product", product["id"]), **value} for value in values]},
                              warnings=list(dict.fromkeys(warnings)))
            payload.meta = {"hotsite": hotsite["slug"], "campos": [value["key"] for value in values]}
            contents.append(payload)

    return {
        "metaobject_definition": metaobject_definitions(),
        "file": list(files.values()),
        "metaobject": list(metaobjects.values()),
        "product_content": contents,
    }
