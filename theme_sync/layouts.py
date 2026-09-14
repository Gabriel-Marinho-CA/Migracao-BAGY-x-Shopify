"""Templates JSON do tema (home, cabecalho, rodape, produto, colecao, paginas).

Tudo que tem imagem passa por ImageLibrary.ref(): o build roda duas vezes, a
primeira so para saber quais imagens pedir, a segunda ja com os nomes finais.
"""

from __future__ import annotations

import copy
import json
import re

from . import bagy
from .config import BAGY_CDN, STATIC_DIR, THEME_DIR

FILES = BAGY_CDN + "files/"
LOGO = FILES + "mad-cor.png?v=1727201017"
FAVICON = FILES + "mad-icon.png?v=1725988740"
ANNOUNCEMENT = "Frete Grátis em compras acima de R$220,00 - Para SP Capital - R$300,00 BRASIL | 5% off no PIX"
COPYRIGHT = ("© MAD 4 Life - Todos os direitos reservados. | CNPJ: 28.728.198/0001-43 | Rodovia Doutor Everaldo "
             "de Sousa Martins, s/n, Alter do Chão - Santarém/PA | CEP - 68020-991")
SEO_TEXT = "Mad 4 Life | Superalimentos direto da Amazônia | Da Floresta ao Consumidor"

# (desktop 1920x500, mobile 600x700, link) na ordem da home da Bagy
HOME_BANNERS = (
    ("drive-po8sxdkf7g2qnsibm7d6-bp0djuycv4ptwzxmkztr-post-mad4life-out25-4-1920x500-03-1.png?v=1769433118",
     "drive-po8sxdkf7g2qnsibm7d6-n49jedbirf4wdwjewtik-post-mad4life-out25-4-600x700-04-1.png?v=1769433123",
     "/collections/spirulina-1"),
    ("drive-po8sxdkf7g2qnsibm7d6-dcqpzsmoiz7yrrexe97i-post-mad4life-out25-1-1920x500-03-3.png?v=1769433935",
     "drive-po8sxdkf7g2qnsibm7d6-smnzj18o7xvunmjsrjbf-post-mad4life-out25-1-600x700-3.png?v=1769434051",
     "/collections/guarana-cipo"),
    ("drive-po8sxdkf7g2qnsibm7d6-snjsmzbr3ewidnev8tld-post-mad4life-out25-1-1920x500-15-2.png?v=1769434067",
     "drive-po8sxdkf7g2qnsibm7d6-9dedoz1dbf061phmps4k-post-mad4life-out25-1-600x700-16-2.png?v=1769434069",
     "/products/shot-mad-guarana-cipo-curcuma-gengibre-100gr-mad"),
    ("drive-po8sxdkf7g2qnsibm7d6-r6zz5qh0parpdqyxt03o-post-mad4life-out25-1-1920x500-07-4.png?v=1769434094",
     "drive-po8sxdkf7g2qnsibm7d6-2go8z53ltgvwsnmm4f30-post-mad4life-out25-1-600x700-08-4.png?v=1769434097",
     "/products/cacau-selvagem-puro-em-po-100g-mad"),
    ("banner-mad-terra-preta-amazonica-htf1v.png?v=1777403090",
     "banner-mad-terra-preta-mobile-enpw8.png?v=1777403570",
     "/pages/terra-preta-amazonica"),
)
HOME_SHOWCASES = (
    ("superalimentos", "Superalimentos", 40),
    ("biocosmeticos", "Biocosméticos", 100),
    ("kits", "Kits", 100),
)
INSTAGRAM = ('<br><br><br><script src="https://cdn.lightwidget.com/widgets/lightwidget.js"></script>'
             '<iframe src="https://cdn.lightwidget.com/widgets/3956ead8d1cc53f9877828943d8777ce.html" '
             'scrolling="no" allowtransparency="true" class="lightwidget-widget" '
             'style="width:100%;border:0;overflow:hidden;"></iframe>')

PRODUCT_SECTIONS = {
    "main": {"type": "mad-main-product", "settings": {"button_label": "Comprar", "note": "Envio imediato"}},
    "buy_together": {"type": "mad-buy-together", "settings": {
        "title": "Aproveite e compre junto", "button_label": "Comprar", "limit": 2, "intent": "related", "pix_percent": 5}},
    "seals": {"type": "mad-product-seals", "settings": {"title": "Certificações e Qualidade"}},
    "reviews": {"type": "mad-product-reviews", "settings": {"title": "Depoimentos e Avaliações"}},
    "nutrition": {"type": "mad-product-nutrition", "settings": {
        "title": "Informações Nutricionais", "table_title": "Tabela nutricional", "specs_title": "Especificações",
        "usage_title": "Como consumir ?", "ingredients_title": "Ingredientes"}},
}

SCHEMES = {
    "scheme-1": {"background": "#EDEDE5", "background_gradient": "", "text": "#334641", "button": "#1C3D15",
                 "button_label": "#FFFFFF", "secondary_button_label": "#1C3D15", "shadow": "#121212"},
    "scheme-2": {"background": "#FFFFFF", "background_gradient": "", "text": "#334641", "button": "#1C3D15",
                 "button_label": "#FFFFFF", "secondary_button_label": "#1C3D15", "shadow": "#121212"},
    "scheme-3": {"background": "#1C3D15", "background_gradient": "", "text": "#FFFFFF", "button": "#FFFFFF",
                 "button_label": "#1C3D15", "secondary_button_label": "#FFFFFF", "shadow": "#121212"},
    "scheme-4": {"background": "#EEEDE9", "background_gradient": "", "text": "#334641", "button": "#1C3D15",
                 "button_label": "#FFFFFF", "secondary_button_label": "#1C3D15", "shadow": "#121212"},
    "scheme-5": {"background": "#334641", "background_gradient": "", "text": "#FFFFFF", "button": "#FFFFFF",
                 "button_label": "#334641", "secondary_button_label": "#FFFFFF", "shadow": "#121212"},
}


def template(sections: list, **extra) -> dict:
    data = dict(extra)
    data["sections"] = {key: copy.deepcopy(value) for key, value in sections}
    data["order"] = [key for key, _ in sections]
    return data


def blocks(items: list) -> dict:
    return {"blocks": {key: value for key, value in items}, "block_order": [key for key, _ in items]}


def compact(settings: dict) -> dict:
    return {key: value for key, value in settings.items() if value not in (None, "")}


# ------------------------------------------------------------------ globais

def index(images) -> dict:
    slides = [(f"slide_{number}", {"type": "slide", "settings": compact({
        "image": images.ref(FILES + desktop), "image_mobile": images.ref(FILES + mobile), "link": link})})
        for number, (desktop, mobile, link) in enumerate(HOME_BANNERS, 1)]
    sections = [("banners", {"type": "mad-banner-carousel", "settings": {
        "autoplay": True, "speed": 5, "show_arrows": True, "show_dots": True}, **blocks(slides)})]
    for handle, title, padding_top in HOME_SHOWCASES:
        sections.append((f"showcase_{handle}", {"type": "mad-product-carousel", "settings": {
            "title": title, "collection": handle, "limit": 10, "per_view": 5, "button_label": "Comprar",
            "padding_top": padding_top, "padding_bottom": 60}}))
    sections.append(("image_text_1", {"type": "mad-image-text", "settings": compact({
        "layout": "image_left",
        "image": images.ref(FILES + "24-09-mad-bannerdestaquesuperalimentos.jpg?v=1727803710"),
        "min_height": 470,
        "heading": "Rotina de autocuidado de dentro para fora com alto valor nutricional.",
        "heading_size": 26,
        "text_html": "<p>✓ supernutrientes<br>✓ 100% puros<br>✓ potencializa sua saúde física e mental<br>"
                     "✓ riqueza biológica</p>",
        "button_label": "Saiba mais", "button_link": "/collections/superalimentos", "background": "#FFFFFF"})}))
    sections.append(("image_text_2", {"type": "mad-image-text", "settings": compact({
        "layout": "image_right",
        "image": images.ref(FILES + "24-09-mad-bannerdestaquemarca.jpg?v=1727803919"),
        "min_height": 470,
        "heading": "A ciência da natureza a favor do seu corpo",
        "heading_size": 26,
        "text_html": "<p>Com produtos que purificam o organismo, regeneram o planeta e geram impacto social "
                     "positivo, a MAD se destaca.</p><p>Desenvolvemos supernutrientes que promovem o bem-estar, "
                     "sempre com respeito ao meio ambiente.</p>",
        "button_label": "Saiba mais", "button_link": "/pages/sobre", "background": "#FFFFFF"})}))
    sections.append(("instagram", {"type": "custom-liquid", "settings": {
        "custom_liquid": INSTAGRAM, "color_scheme": "scheme-1", "padding_top": 0, "padding_bottom": 40}}))
    return template(sections)


def header_group() -> dict:
    return template([
        ("announcement-bar", {"type": "announcement-bar", **blocks([
            ("announcement-bar-0", {"type": "announcement", "settings": {"text": ANNOUNCEMENT, "link": ""}})]),
            "settings": {"auto_rotate": False, "change_slides_speed": 5, "color_scheme": "scheme-3",
                         "show_line_separator": False, "show_social": False,
                         "enable_country_selector": False, "enable_language_selector": False}}),
        ("header", {"type": "header", "settings": {
            "logo_position": "middle-left", "mobile_logo_position": "left", "menu": "main-menu",
            "menu_type_desktop": "dropdown", "sticky_header_type": "always", "show_line_separator": False,
            "color_scheme": "scheme-2", "menu_color_scheme": "scheme-2",
            "enable_country_selector": False, "enable_language_selector": False,
            "margin_bottom": 0, "padding_top": 0, "padding_bottom": 0}}),
    ], name="t:sections.header.name", type="header")


def footer_group(images) -> dict:
    footer_blocks = blocks([
        ("menu_novo", {"type": "menu", "settings": {"heading": "Menu Novo", "menu": "mad-menu-novo"}}),
        ("suporte", {"type": "menu", "settings": {"heading": "Suporte", "menu": "mad-suporte"}}),
        ("sobre", {"type": "menu", "settings": {"heading": "Sobre Nós", "menu": "mad-sobre-nos"}}),
        ("atendimento", {"type": "contact", "settings": {"heading": "Central de Atendimento", "email": "contato@mad4.life"}}),
    ])
    return template([
        ("mad_footer", {"type": "mad-footer", "settings": compact({
            "logo": images.ref(LOGO),
            "facebook": "https://pt-br.facebook.com/mad4.life1/",
            "instagram": "https://www.instagram.com/mad4life/",
            "show_newsletter": True,
            "newsletter_title": "Receba novidades e promoções",
            "payments_title": "Pagamento",
            "payments_image": images.ref(STATIC_DIR / "pagamentos.png"),
            "security_title": "Segurança",
            "seal_1": images.ref(STATIC_DIR / "seguranca.png"),
            "seo_text": SEO_TEXT,
            "copyright": COPYRIGHT}), **footer_blocks}),
    ], name="t:sections.footer.name", type="footer")


def settings_data(images) -> dict:
    path = THEME_DIR / "config" / "settings_data.json"
    raw = json.loads(strip_comment(path.read_text(encoding="utf-8")))
    current = raw.get("current")
    if isinstance(current, str):
        current = copy.deepcopy(raw.get("presets", {}).get(current, {}))
    current.update(compact({
        "logo": images.ref(LOGO),
        "favicon": images.ref(FAVICON),
    }))
    current.update({
        "logo_width": 140,
        "color_schemes": {scheme: {"settings": values} for scheme, values in SCHEMES.items()},
        # Handles validos do font_picker; as familias reais (Federo/Nanum Gothic) vem de mad-fonts + mad-theme.css.
        "type_header_font": "assistant_n4",
        "type_body_font": "assistant_n4",
        "page_width": 1600,
        "spacing_sections": 0,
        "buttons_radius": 0,
        "inputs_radius": 0,
        "card_color_scheme": "scheme-1",
        "collection_card_color_scheme": "scheme-1",
        "blog_card_color_scheme": "scheme-1",
        "sale_badge_color_scheme": "scheme-3",
        "badge_corner_radius": 0,
        "currency_code_enabled": False,
        "cart_type": "notification",
        "cart_color_scheme": "scheme-2",
        "social_facebook_link": "https://pt-br.facebook.com/mad4.life1/",
        "social_instagram_link": "https://www.instagram.com/mad4life/",
        "animations_reveal_on_scroll": False,
    })
    raw["current"] = current
    return raw


def strip_comment(text: str) -> str:
    return re.sub(r"^\s*/\*.*?\*/", "", text, count=1, flags=re.S)


# ------------------------------------------------------------ por tipo

def product_default() -> dict:
    return template([(key, PRODUCT_SECTIONS[key]) for key in ("main", "buy_together", "seals", "reviews", "nutrition")])


def product_from_hotsite(hotsite: dict, images) -> dict | None:
    body = bagy.Converter(images, "product").run(hotsite)
    if not any(value not in ("seals", "reviews") for _, value in body):
        return None
    sections = [("main", PRODUCT_SECTIONS["main"]), ("buy_together", PRODUCT_SECTIONS["buy_together"])]
    used = set()
    for key, value in body:
        if value in ("seals", "reviews"):
            if value not in used:
                used.add(value)
                sections.append((value, PRODUCT_SECTIONS[value]))
        else:
            sections.append((key, value))
    for name in ("seals", "reviews"):
        if name not in used:
            sections.append((name, PRODUCT_SECTIONS[name]))
    sections.append(("nutrition", PRODUCT_SECTIONS["nutrition"]))
    return template(sections)


def page_from_hotsite(hotsite: dict, images) -> dict | None:
    body = bagy.Converter(images, "page").run(hotsite)
    if not any(value.get("type") != "mad-main-page" for _, value in body):
        return None
    return template(body)


def collection() -> dict:
    return template([("main", {"type": "mad-main-collection", "settings": {
        "per_page": 24, "columns": 3, "button_label": "Comprar", "show_description": False}})])


def page(contact: bool = False) -> dict:
    settings = {"layout": "sidebar", "menu": "mad-institucional", "show_contact_form": contact}
    if contact:
        settings["heading"] = "Fale Conosco"
        settings["contact_heading"] = "Atendimento por e-mail"
        settings["contact_intro"] = ('<p>Se preferir, entre em contato direto através do e-mail '
                                     '<a href="mailto:contato@mad4.life">contato@mad4.life</a></p>')
    return template([("main", {"type": "mad-main-page", "settings": settings})])


def blog() -> dict:
    return template([("main", {"type": "mad-main-blog", "settings": {"per_page": 12}})])


def article() -> dict:
    return template([("main", {"type": "mad-main-article", "settings": {}})])


# ----------------------------------------------------------------- build

def build(images) -> tuple:
    """(arquivos {caminho no tema: dados}, atribuicoes {"products": {handle: sufixo}, "pages": {...}})."""
    files = {
        "templates/index.json": index(images),
        "templates/collection.json": collection(),
        "templates/product.json": product_default(),
        "templates/page.json": page(),
        "templates/page.contact.json": page(contact=True),
        "templates/blog.json": blog(),
        "templates/article.json": article(),
        "sections/header-group.json": header_group(),
        "sections/footer-group.json": footer_group(images),
        "config/settings_data.json": settings_data(images),
    }
    assignments = {"products": {}, "pages": {"contact": "contact"}}
    links = bagy.product_links()
    for slug, hotsite in sorted(bagy.active_hotsites().items()):
        products = links.get(hotsite["id"])
        if products:
            data = product_from_hotsite(hotsite, images)
            if data:
                files[f"templates/product.{slug}.json"] = data
                for handle in products:
                    assignments["products"][handle] = slug
        else:
            data = page_from_hotsite(hotsite, images)
            if data:
                files[f"templates/page.{slug}.json"] = data
                assignments["pages"][slug] = slug
    return files, assignments


def write(files: dict, log=print) -> list:
    changed = []
    for name, data in files.items():
        path = THEME_DIR / name
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        changed.append(name)
        log(f"  escrito {name}")
    return changed
