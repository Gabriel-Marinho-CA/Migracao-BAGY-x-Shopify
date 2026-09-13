"""Conteudo: blog, artigos, politicas, paginas, hotsites, menus e redirecionamentos."""

from __future__ import annotations

import html
import re
from urllib.parse import quote

from ..common import (
    clean, closest_path, compact, image_src, iso, metafield, one_line, ref, resolve_media_in_html,
    rewrite_links, rewrite_url, seo_metafields, slugify, store_path, tag_list, truncate,
)
from ..model import Payload, skipped

# Paginas da Bagy que na Shopify sao politicas da loja (Configuracoes > Politicas).
POLICY_PAGES = {
    "politica-de-privacidade": ("PRIVACY_POLICY", "/policies/privacy-policy"),
    "trocas-e-devolucoes": ("REFUND_POLICY", "/policies/refund-policy"),
    "politica-de-entrega": ("SHIPPING_POLICY", "/policies/shipping-policy"),
}

APPROXIMATE = "destino nao existe mais - apontado para o caminho mais parecido (conferir)"


def _with_links(payload: Payload, body, mapping, hosts):
    """Aplica a reescrita de links internos e contabiliza no relatorio."""
    if not body or not mapping:
        return body
    body, count = rewrite_links(body, mapping, hosts)
    if count:
        payload.meta["links_rewritten"] = payload.meta.get("links_rewritten", 0) + count
    return body


# --------------------------------------------------------------------- blog


def blog(src, settings) -> list:
    return [Payload("blog", "blog:main", "blogCreate",
                    {"blog": {"title": settings.blog_title, "handle": settings.blog_handle,
                              "commentPolicy": "CLOSED"}},
                    provides=[ref("blog", "main")])]


def articles(src, settings, mapping: dict | None = None) -> list:
    """Um blog so, com a categoria do post como tag.

    A Bagy tem 8 categorias para 24 posts; na Shopify, tag gera a URL
    /blogs/blog/tagged/<categoria>, que substitui a pagina de categoria.
    """
    shop_id = src.shop_id
    categories = {category["id"]: category for category in src.all("post_categories")}
    payloads = []
    for post in src.all("posts"):
        key = f"post:{post['id']}"
        payload = Payload("article", key, "articleCreate", provides=[ref("article", post["id"])])
        category = categories.get(post.get("post_category_id")) or post.get("category") or {}
        image = image_src(post.get("image"), shop_id)
        body = _with_links(payload, resolve_media_in_html(post.get("description"), shop_id),
                           mapping, src.store_hosts)
        payload.variables = {"article": compact({
            "blogId": ref("blog", "main"),
            "title": post["name"],
            "handle": post["slug"],
            "body": body,
            "summary": one_line(post.get("meta_description")),
            "image": compact({"url": image,
                              "altText": clean((post.get("image") or {}).get("alt")) or post["name"]})
                     if image else None,
            "tags": tag_list(*(post.get("tags") or "").split(","), category.get("name")) or None,
            "isPublished": bool(post.get("active")),
            "publishDate": iso(post.get("created_at"), settings.timezone_offset),
            "author": {"name": settings.article_author},
            "metafields": seo_metafields(post.get("meta_title"), post.get("meta_description")) or None,
        })}
        payloads.append(payload)
    return payloads


# ------------------------------------------------------------------ paginas


def pages(src, settings, mapping: dict | None = None) -> list:
    shop_id = src.shop_id
    payloads = []
    for page in src.all("pages"):
        key = f"page:{page['id']}"
        slug = page["slug"]

        if slug in POLICY_PAGES:
            policy_type = POLICY_PAGES[slug][0]
            payload = Payload("policy", key, "shopPolicyUpdate", provides=[ref("policy", policy_type)])
            body = _with_links(payload, resolve_media_in_html(page.get("description"), shop_id),
                               mapping, src.store_hosts)
            if not body:
                payloads.append(skipped("policy", key, "politica sem conteudo na Bagy"))
                continue
            payload.variables = {"shopPolicy": {"type": policy_type, "body": body}}
            payloads.append(payload)
            continue

        if slug == settings.blog_handle:
            payloads.append(skipped("page", key, "a pagina Blog da Bagy vira o proprio blog da Shopify"))
            continue

        payload = Payload("page", key, "pageCreate", provides=[ref("page", key)])
        body = _with_links(payload, resolve_media_in_html(page.get("description"), shop_id),
                           mapping, src.store_hosts)
        payload.variables = {"page": compact({
            "title": page["name"],
            "handle": slug,
            "body": body or "",
            "isPublished": bool(page.get("active")),
            "templateSuffix": "contact" if page.get("template") == "contact" else None,
            "metafields": seo_metafields(page.get("meta_title"), page.get("meta_description")) or None,
        })}
        if page.get("template") == "contact":
            payload.warn("pagina de contato - conferir se o tema tem o template page.contact")
        if page.get("faq"):
            payload.warn("FAQ da pagina nao migrado")
        payloads.append(payload)
    return payloads


def hotsite_handles(src) -> dict:
    page_slugs = {page["slug"] for page in src.all("pages")}
    return {hotsite["id"]: (f"{hotsite['slug']}-lp" if hotsite["slug"] in page_slugs else hotsite["slug"])
            for hotsite in src.all("hotsites")}


def product_hotsite_targets(src) -> dict:
    """hotsite_id -> produto que o representa (o primeiro ativo)."""
    from .product_content import product_hotsites
    return {hotsite_id: products[0] for hotsite_id, products in product_hotsites(src).items()}


def hotsite_pages(src, settings, mapping: dict | None = None) -> list:
    shop_id = src.shop_id
    handles = hotsite_handles(src)
    of_products = product_hotsite_targets(src)
    payloads = []
    for hotsite in src.all("hotsites"):
        key = f"hotsite:{hotsite['id']}"
        if not hotsite.get("active"):
            payloads.append(skipped("page", key, "hotsite inativo na Bagy"))
            continue
        if hotsite["id"] in of_products:
            payloads.append(skipped("page", key, "hotsite de produto - o conteudo virou metaobjetos do produto"))
            continue
        body, warnings = render_hotsite(hotsite, shop_id)
        payload = Payload("page", key, "pageCreate", provides=[ref("page", key)], warnings=warnings)
        body = _with_links(payload, body, mapping, src.store_hosts)
        payload.warn("conteudo convertido do editor visual da Bagy - revisar o visual na Shopify")
        if handles[hotsite["id"]] != hotsite["slug"]:
            payload.warn("handle ganhou sufixo -lp por conflito com uma pagina")
        metafields = seo_metafields(hotsite.get("meta_title"), hotsite.get("meta_description"))
        if hotsite.get("not_index"):
            metafields.append(metafield("seo", "hidden", "number_integer", 1))
        payload.variables = {"page": compact({
            "title": hotsite["name"],
            "handle": handles[hotsite["id"]],
            "body": body or "",
            "isPublished": True,
            "metafields": metafields or None,
        })}
        payloads.append(payload)
    return payloads


# ---------------------------------------------------- render dos hotsites


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _rich(value) -> str:
    text = clean(value)
    if not text:
        return ""
    return text if "<" in text else f"<p>{_esc(text)}</p>"


def _img(value, shop_id, alt=None) -> str:
    src = image_src(value, shop_id)
    return f'<img src="{_esc(src)}" alt="{_esc(clean(alt) or "")}" loading="lazy">' if src else ""


def _link(url, text, blank=False) -> str:
    url, text = clean(url), clean(text)
    if not url or not text:
        return ""
    target = ' target="_blank" rel="noopener"' if blank in (True, "_blank") else ""
    return f'<p><a class="button" href="{_esc(url)}"{target}>{_esc(text)}</a></p>'


def _heading(settings: dict) -> str:
    parts = []
    if clean(settings.get("title")):
        parts.append(f"<h2>{_esc(settings['title'])}</h2>")
    if clean(settings.get("subtitle")):
        parts.append(f'<p class="subtitle">{_esc(settings["subtitle"])}</p>')
    return "\n".join(parts)


def _title(value, tag="h3") -> str:
    return f"<{tag}>{_esc(value)}</{tag}>" if clean(value) else ""


def _video(url) -> str:
    url = clean(url)
    if not url:
        return ""
    match = re.search(r"(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)([\w-]{6,})", url)
    if match:
        return (f'<iframe src="https://www.youtube.com/embed/{match.group(1)}" '
                'loading="lazy" allowfullscreen></iframe>')
    match = re.search(r"vimeo\.com/(?:video/)?(\d+)", url)
    if match:
        return (f'<iframe src="https://player.vimeo.com/video/{match.group(1)}" '
                'loading="lazy" allowfullscreen></iframe>')
    return f'<p><a href="{_esc(url)}">{_esc(url)}</a></p>'


def _join(*parts) -> str:
    return "\n".join(part for part in parts if part)


def _render_image_text(settings, blocks, shop_id) -> str:
    cards = [_join(_img(b.get("image"), shop_id, b.get("title")), _title(b.get("title")),
                   _rich(b.get("text")),
                   _link(b.get("button_url"), b.get("button_text"), b.get("button_blank")))
             for b in blocks]
    return _join(_heading(settings), *(f'<div class="bagy-block">\n{c}\n</div>' for c in cards if c))


def _render_banners(image_key: str, url_key: str, text_key: str):
    def render(settings, blocks, shop_id) -> str:
        cards = []
        for block in blocks:
            inner = _join(_img(block.get(image_key), shop_id, block.get("title")),
                          _title(block.get("title")), _rich(block.get(text_key)))
            if not inner:
                continue
            url = clean(block.get(url_key))
            cards.append(f'<div class="bagy-banner"><a href="{_esc(url)}">\n{inner}\n</a></div>'
                         if url else f'<div class="bagy-banner">\n{inner}\n</div>')
        return _join(_heading(settings), *cards)
    return render


def _render_action_button(settings, blocks, shop_id) -> str:
    return _join(_heading(settings),
                 *(_link(b.get("button_url"), b.get("button_text"), b.get("button_blank")) for b in blocks))


def _render_overlaid(settings, blocks, shop_id) -> str:
    return _join(_heading(settings), _img(settings.get("background_image"), shop_id, settings.get("title")),
                 _rich(settings.get("text")),
                 _link(settings.get("button_url"), settings.get("button_text"), settings.get("button_target")))


def _render_video_text(settings, blocks, shop_id) -> str:
    return _join(_heading(settings), *(_join(_title(b.get("title")), _rich(b.get("description")),
                                             _video(b.get("video"))) for b in blocks))


def _render_reviews(settings, blocks, shop_id) -> str:
    quotes = []
    for block in blocks:
        stars = "★" * int(block.get("star_rating") or 0)
        body = _join(_rich(block.get("description")),
                     f"<cite>{_esc(block['client'])}</cite>" if clean(block.get("client")) else "",
                     f'<p class="stars">{stars}</p>' if stars else "")
        if body:
            quotes.append(f"<blockquote>\n{body}\n</blockquote>")
    return _join(_heading(settings), *quotes)


def _render_youtube(settings, blocks, shop_id) -> str:
    channel = clean(settings.get("channel_id"))
    link = _link(f"https://www.youtube.com/channel/{channel}",
                 settings.get("button_text") or "Ver canal no YouTube", True) if channel else ""
    return _join(_heading(settings), link)


RENDERERS = {
    "html": lambda settings, blocks, shop_id: clean(settings.get("content")) or "",
    "image-text": _render_image_text,
    "image-highlights-topics": lambda s, b, sid: _join(_img(s.get("main_image"), sid, s.get("title")),
                                                       _render_image_text(s, b, sid)),
    "banners": _render_banners("image", "banner_url", "text"),
    "banners-commercial": _render_banners("image", "url", "description"),
    "banners-carousel": _render_banners("src", "banner_url", "content"),
    "action-button": _render_action_button,
    "image-text-overlaid": _render_overlaid,
    "video-text": _render_video_text,
    "video": lambda s, b, sid: _join(_heading(s), _video(s.get("video"))),
    "youtube": _render_youtube,
    "reviews": _render_reviews,
}


def render_hotsite(hotsite: dict, shop_id) -> tuple:
    content = hotsite.get("content") or {}
    sections = content.get("sections") or {}
    ordered = [key for key in content.get("content_for_index") or [] if key in sections]
    ordered += [key for key in sections if key not in ordered]

    parts, warnings = [], []
    for key in ordered:
        section = sections[key] or {}
        kind = section.get("type")
        renderer = RENDERERS.get(kind)
        if renderer is None:
            message = f"secao '{kind}' sem equivalente em pagina - recriar no tema"
            if message not in warnings:
                warnings.append(message)
            continue
        blocks = [block.get("settings") or {} for block in section.get("blocks") or []]
        rendered = renderer(section.get("settings") or {}, blocks, shop_id)
        if rendered:
            parts.append(f'<section class="bagy-{_esc(kind)}">\n{rendered}\n</section>')

    body = resolve_media_in_html("\n".join(parts), shop_id) or ""
    return body, warnings


# ------------------------------------------------------- URLs e redirects


def path_map(src, settings) -> dict:
    """URL antiga da Bagy (namespace plano /<slug>) -> caminho na Shopify."""
    blog_handle = settings.blog_handle
    mapping: dict = {}

    def add(slug, target) -> None:
        path = store_path(slug)
        if path != "/" and path not in mapping:
            mapping[path] = target

    for product in src.all("products"):
        add(product["slug"], f"/products/{product['slug']}")
    for category in src.all("categories"):
        add(category["slug"], f"/collections/{category['slug']}")
    for brand in src.all("brands"):
        add(brand["slug"], f"/collections/vendors?q={quote(brand['name'])}")
    for post in src.all("posts"):
        add(post["slug"], f"/blogs/{blog_handle}/{post['slug']}")
    for category in src.all("post_categories"):
        add(category["slug"], f"/blogs/{blog_handle}/tagged/{slugify(category['name'])}")
    for page in src.all("pages"):
        if page["slug"] in POLICY_PAGES:
            add(page["slug"], POLICY_PAGES[page["slug"]][1])
        elif page["slug"] == blog_handle:
            add(page["slug"], f"/blogs/{blog_handle}")
        else:
            add(page["slug"], f"/pages/{page['slug']}")
    handles = hotsite_handles(src)
    of_products = product_hotsite_targets(src)
    for hotsite in src.all("hotsites"):
        if hotsite["id"] in of_products:
            # Hotsite de produto nao vira pagina: a URL antiga leva ao produto.
            add(hotsite["slug"], f"/products/{of_products[hotsite['id']]['slug']}")
        elif hotsite.get("active"):
            add(hotsite["slug"], f"/pages/{handles[hotsite['id']]}")
    # Destino usado pelos redirects da propria Bagy.
    add("produtos", "/collections/all")
    return mapping


def menus(src, settings, mapping: dict) -> list:
    pages_by_id = {page["id"]: page for page in src.all("pages")}
    active_hotsites = {hotsite["id"] for hotsite in src.all("hotsites") if hotsite.get("active")}
    of_products = product_hotsite_targets(src)
    categories = {category["id"] for category in src.all("categories")}
    hosts = src.store_hosts
    payloads = []

    for menu in src.all("menus"):
        payload = Payload("menu", f"menu:{menu['id']}", "menuCreate", provides=[ref("menu", menu["id"])])

        def build(value: dict):
            if not value.get("active", True):
                return None
            children = [item for item in (build(child) for child in value.get("children") or []) if item]
            kind = value.get("menu_type")
            node = None
            if kind == "category" and value.get("category_id") in categories:
                node = {"type": "COLLECTION", "resourceId": ref("collection", f"category:{value['category_id']}")}
            elif kind == "page" and value.get("page_id") in pages_by_id:
                slug = pages_by_id[value["page_id"]]["slug"]
                if slug in POLICY_PAGES:
                    node = {"type": "SHOP_POLICY", "resourceId": ref("policy", POLICY_PAGES[slug][0])}
                elif slug == settings.blog_handle:
                    node = {"type": "BLOG", "resourceId": ref("blog", "main")}
                else:
                    node = {"type": "PAGE", "resourceId": ref("page", f"page:{value['page_id']}")}
            elif kind == "hotsite" and value.get("hotsite_id") in of_products:
                node = {"type": "PRODUCT", "resourceId": ref("product", of_products[value["hotsite_id"]]["id"])}
            elif kind == "hotsite" and value.get("hotsite_id") in active_hotsites:
                node = {"type": "PAGE", "resourceId": ref("page", f"hotsite:{value['hotsite_id']}")}
            else:
                url = clean(value.get("link")) or clean(value.get("menu_type_link"))
                if url:
                    target, resolved = rewrite_url(url, mapping, hosts)
                    if not resolved:
                        near = closest_path(target, mapping)
                        if near:
                            target = mapping[near]
                            payload.warn(f"link de menu: {APPROXIMATE}")
                        else:
                            payload.warn("link de menu aponta para pagina da Bagy sem equivalente")
                    node = {"type": "HTTP", "url": target}
            if node is None:
                payload.warn("item de menu sem destino migravel - removido")
                return None
            return compact({"title": truncate(value.get("name"), 255) or "Link", **node,
                            "items": children or None})

        items = [item for item in (build(value) for value in menu.get("values") or []) if item]
        payload.variables = {"title": menu["name"], "handle": menu["handle"], "items": items}
        payloads.append(payload)
    return payloads


def redirects(src, settings, mapping: dict) -> list:
    hosts = src.store_hosts
    payloads, seen = [], set()

    # 1. Redirects cadastrados na Bagy, com o destino reescrito (apontavam para *.bagypro.com).
    for redirect in src.all("redirects"):
        key = f"redirect:{redirect['id']}"
        path = store_path(redirect.get("url_from"))
        if path == "/" or path in seen:
            payloads.append(skipped("redirect", key, "caminho de origem repetido ou vazio"))
            continue
        payload = Payload("redirect", key, "urlRedirectCreate")
        target, resolved = rewrite_url(redirect.get("url_to"), mapping, hosts)
        if not resolved:
            # Destino apontava para um slug que mudou depois (ex.: produto 44g renomeado para 45g).
            near = closest_path(target, mapping)
            if near:
                target = mapping[near]
                payload.warn(f"redirect: {APPROXIMATE}")
            else:
                target = "/"
                payload.warn("destino do redirect nao existe mais - apontado para a home")
        payload.variables = {"urlRedirect": {"path": path, "target": target}}
        seen.add(path)
        payloads.append(payload)

    # 2. Toda URL antiga da loja para o novo caminho na Shopify.
    for old, new in mapping.items():
        if old in seen or old == new:
            continue
        seen.add(old)
        payloads.append(Payload("redirect", f"url:{old}", "urlRedirectCreate",
                                {"urlRedirect": {"path": old, "target": new}}))
    return payloads
