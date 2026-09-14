"""Hotsites da Bagy (editor visual) -> secoes do tema.

So entra o que o tema tem como mostrar: imagem + texto, imagem de fundo com
texto, banners e colunas de texto/HTML. Tabela nutricional, modo de uso,
ingredientes, especificacoes, selos e depoimentos ja sao metafields e
metaobjetos do produto (bagy_transform/entities/product_content.py) e aparecem
pelas secoes mad-product-*: no hotsite de produto esses blocos ficam de fora e
viram marcadores de posicao ("seals", "reviews").
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import defaultdict

from .config import BAGY_CDN, BAGY_DB

PLACEHOLDER = re.compile(r"insira um (t[ií]tulo|subt[ií]tulo)|bot[aã]o de a[cç][aã]o", re.I)
PRODUCT_FIELD = re.compile(r"nutri|consum|modo de uso|como usar|ingredient", re.I)
STRUCTURED_HTML = re.compile(r"<table|<details", re.I)
EMBED = re.compile(r"<iframe|<script|<form|<img|<video", re.I)
PLACEHOLDER_IMAGE = "arquivos/place-holder"
PAGE_BACKGROUND = "#EDEDE5"
# Imagens da Bagy dentro de HTML (src, srcset, url() de CSS).
MEDIA_URL = re.compile(
    r"(?:(?:https?:)?//cdn\.dooca\.store/\d+/|media://)[^\"'\s)<>]+?\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\"'\s)<>]*)?",
    re.I)
PLACEHOLDER_TAG = re.compile(r"<img\b[^>]*arquivos/place-holder[^>]*>", re.I)


def records(resource: str) -> list:
    with sqlite3.connect(BAGY_DB) as conn:
        rows = conn.execute("select payload from records where resource = ? order by record_key", (resource,))
        return [json.loads(payload) for (payload,) in rows]


def image_source(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("src")
    if not isinstance(value, str) or not value.strip() or PLACEHOLDER_IMAGE in value:
        return None
    value = value.strip()
    if value.startswith("media://"):
        return BAGY_CDN + value[len("media://"):]
    if value.startswith("//"):
        return "https:" + value
    return value if value.startswith("http") else None


def heading_text(value) -> str:
    text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def body_html(value, keep_style: bool = False) -> str:
    text = str(value or "")
    if not keep_style:
        text = re.sub(r"<style\b.*?</style>", "", text, flags=re.I | re.S)
    return text.strip()


def rewrite_media(html_text: str, images) -> str:
    """Tira as imagens de exemplo da Bagy e troca as do CDN da Bagy pelas do Files da Shopify.

    Imagem que ainda nao subiu fica com a URL da Bagy (e fica pedida na biblioteca).
    """
    text = PLACEHOLDER_TAG.sub("", html_text or "")

    def swap(match):
        source = image_source(match.group(0))
        return (images.url(source) or source) if source else match.group(0)

    return MEDIA_URL.sub(swap, text)


def color(value, default=None):
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        return value.strip().upper()
    return default


def sections(hotsite: dict) -> list:
    content = hotsite.get("content") or {}
    found = content.get("sections") or {}
    order = [key for key in content.get("content_for_index") or [] if key in found]
    order += [key for key in found if key not in order]
    excluded = {key for key in content.get("sections_exclude") or [] if isinstance(key, str)}
    return [found[key] or {} for key in order if key not in excluded]


def active_hotsites() -> dict:
    return {hotsite["slug"]: hotsite for hotsite in records("hotsites") if hotsite.get("active") and hotsite.get("slug")}


def product_links() -> dict:
    """hotsite_id -> slugs dos produtos que usam o hotsite."""
    linked = defaultdict(list)
    for product in records("products"):
        if product.get("hotsite_id") and product.get("slug"):
            linked[product["hotsite_id"]].append(product["slug"])
    return linked


class Converter:
    def __init__(self, images, kind: str):
        self.images = images
        self.kind = kind
        self.heading_size = 40 if kind == "product" else 26
        self.height = 720 if kind == "product" else 470
        self.overlay_height = 1200 if kind == "product" else 600
        self.seen: set = set()

    def run(self, hotsite: dict) -> list:
        out = []
        for index, section in enumerate(sections(hotsite), 1):
            kind = section.get("type")
            settings = section.get("settings") or {}
            blocks = [(block or {}).get("settings") or {} for block in section.get("blocks") or []]
            key = f"bagy_{index}"
            if kind == "image-text":
                out.extend(self._image_text(key, settings, blocks))
            elif kind == "banners-carousel":
                out.extend(self._banners(key, settings, blocks))
            elif kind == "banners-commercial" and self.kind == "product":
                if any(image_source(block.get("image")) for block in blocks):
                    out.append(("seals", "seals"))
            elif kind == "reviews" and self.kind == "product":
                out.append(("reviews", "reviews"))
            elif kind == "action-button" and self.kind == "page":
                title = heading_text(settings.get("title"))
                if title:
                    out.append(("title", {"type": "mad-main-page", "settings": {"layout": "title_only", "heading": title}}))
            elif kind == "html" and self.kind == "page":
                visible = body_html(settings.get("content"))
                if heading_text(visible) or EMBED.search(visible):
                    # Secao HTML de pagina (ex.: Terra Preta) traz o proprio CSS: so vale naquela pagina.
                    content = body_html(settings.get("content"), keep_style=True)
                    out.append((f"{key}_html", self.text_columns("", [("", content)], settings)))
        return out

    def _image_text(self, key: str, settings: dict, blocks: list) -> list:
        out, texts = [], []
        background_image = image_source(settings.get("background_image"))
        for number, block in enumerate(blocks, 1):
            heading = heading_text(block.get("title"))
            text = body_html(block.get("text"))
            if not heading and not heading_text(text) and not EMBED.search(text):
                continue
            if PLACEHOLDER.search(heading) or PLACEHOLDER.search(heading_text(text)):
                continue
            if self.kind == "product" and (PRODUCT_FIELD.search(heading) or STRUCTURED_HTML.search(text)):
                continue
            if not self._first_time(f"{heading} {text}"):
                continue
            image = image_source(block.get("image"))
            if image:
                layout = "image_right" if settings.get("align") == "flex-row-reverse" else "image_left"
                out.append((f"{key}_{number}", self.image_text(image, layout, heading, text, settings)))
            elif background_image:
                out.append((f"{key}_{number}", self.image_text(
                    background_image, "overlay", heading, text, settings, height=self.overlay_height)))
            else:
                texts.append((heading, text))
        if texts:
            out.append((f"{key}_text", self.text_columns(heading_text(settings.get("title")), texts, settings)))
        return out

    def _banners(self, key: str, settings: dict, blocks: list) -> list:
        out, slides = [], []
        for number, block in enumerate(blocks, 1):
            image = image_source(block.get("src"))
            text = body_html(block.get("title") or block.get("content"))
            if not image:
                continue
            if heading_text(text):
                if self._first_time(text):
                    out.append((f"{key}_{number}", self.image_text(
                        image, "overlay", "", text, settings, height=self.overlay_height)))
            else:
                slides.append((image, image_source(block.get("src_mobile")), block.get("banner_url")))
        if slides:
            out.append((f"{key}_banners", self.banners(slides)))
        return out

    def _first_time(self, text: str) -> bool:
        marker = " ".join(heading_text(text).split())[:300]
        if not marker:
            return True
        if marker in self.seen:
            return False
        self.seen.add(marker)
        return True

    # -------------------------------------------------------------- sections

    def image_text(self, image: str, layout: str, heading: str, text: str, settings: dict, height=None) -> dict:
        data = {
            "layout": layout,
            "heading": heading,
            "heading_size": self.heading_size,
            "text_html": rewrite_media(text, self.images),
            "min_height": height or self.height,
            "background": color(settings.get("background_color"), PAGE_BACKGROUND),
        }
        ref = self.images.ref(image)
        if ref:
            data["image"] = ref
        else:
            data["image_src"] = image
        if color(settings.get("title_color")) and layout != "overlay":
            data["heading_color"] = color(settings.get("title_color"))
        if color(settings.get("subtitle_color")) and layout != "overlay":
            data["text_color"] = color(settings.get("subtitle_color"))
        return {"type": "mad-image-text", "settings": data}

    def text_columns(self, heading: str, columns: list, settings: dict) -> dict:
        blocks, order = {}, []
        for number, (title, content) in enumerate(columns, 1):
            blocks[f"column_{number}"] = {"type": "column", "settings": {
                "heading": title, "html": rewrite_media(content, self.images)}}
            order.append(f"column_{number}")
        data = {"heading": heading, "alignment": "left", "padding_top": 60, "padding_bottom": 60}
        if color(settings.get("background_color")):
            data["background"] = color(settings.get("background_color"))
        return {"type": "mad-text-columns", "settings": data, "blocks": blocks, "block_order": order}

    def banners(self, slides: list) -> dict:
        blocks, order = {}, []
        for number, (desktop, mobile, link) in enumerate(slides, 1):
            data = {}
            if self.images.ref(desktop):
                data["image"] = self.images.ref(desktop)
            if mobile and self.images.ref(mobile):
                data["image_mobile"] = self.images.ref(mobile)
            if link:
                data["link"] = link
            blocks[f"slide_{number}"] = {"type": "slide", "settings": data}
            order.append(f"slide_{number}")
        return {"type": "mad-banner-carousel", "settings": {"autoplay": True, "speed": 5, "show_arrows": True, "show_dots": True},
                "blocks": blocks, "block_order": order}
