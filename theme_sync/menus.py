"""Menus da loja: principal (com submenus) e os do rodape/paginas institucionais.

Handles proprios (mad-*) para nao colidir com os menus que run_load.py criaria
a partir da Bagy. O main-menu padrao da Shopify e atualizado no lugar. Paginas,
blog e politicas entram como link (HTTP): funcionam assim que existirem na loja.
"""

from __future__ import annotations


def collection(title: str, handle: str, *items) -> dict:
    return {"title": title, "type": "COLLECTION", "handle": handle, "items": list(items)}


def link(title: str, url: str, *items) -> dict:
    return {"title": title, "type": "HTTP", "url": url, "items": list(items)}


SUPERALIMENTOS = collection(
    "Superalimentos", "superalimentos",
    collection("Guaraná", "guarana-cipo"),
    collection("Spirulina", "spirulina-1"),
    collection("Pimentas Indígenas", "pimentas-indigenas"),
    collection("Cúrcuma", "curcuma-1"),
    collection("Cacau", "cacau-1"),
    collection("Gengibre", "gengibre-mangarataia"),
    collection("Shot", "shot-1"),
)
BIOCOSMETICOS = collection(
    "Biocosméticos", "biocosmeticos",
    collection("Copaíba", "copaiba-1"),
    collection("Cupuaçu", "cupuacu"),
    collection("Andiroba", "andiroba-1"),
)
PRESCRITORES = link("Prescritores", "/pages/profissional-prescritor")
SOBRE = link("Sobre", "/pages/sobre")
BLOG = link("Blog", "/blogs/blog")
PLANO_MAD = link("Plano MAD", "/pages/plano-mad")
EMPRESAS = link("Empresas", "http://www-mad4-life-com.rds.land/mad-empresas")
ENTREGA = link("Política de Entrega", "/policies/shipping-policy")
TROCAS = link("Trocas e Devoluções", "/policies/refund-policy")
PRIVACIDADE = link("Política de Privacidade", "/policies/privacy-policy")
FALE_CONOSCO = link("Fale Conosco", "/pages/fale-conosco")

MENUS = (
    ("main-menu", "Menu principal", [SUPERALIMENTOS, BIOCOSMETICOS, collection("Kits", "kits"),
                                     PRESCRITORES, SOBRE, BLOG, PLANO_MAD, EMPRESAS]),
    ("mad-menu-novo", "Menu Novo", [collection("Superalimentos", "superalimentos"),
                                    collection("Biocosméticos", "biocosmeticos"), collection("Kits", "kits"),
                                    PRESCRITORES, SOBRE, BLOG, PLANO_MAD, EMPRESAS]),
    ("mad-suporte", "Suporte", [ENTREGA, TROCAS, PRIVACIDADE, FALE_CONOSCO]),
    ("mad-sobre-nos", "Sobre Nós", [link("Quem Somos", "/pages/sobre"),
                                    link("Para Empresas", "https://empresas.mad4.life/"),
                                    link("Para Prescritores", "/pages/profissional-prescritor"),
                                    link("Diretrizes Organizacionais", "/pages/diretrizes-organizacionais")]),
    ("mad-institucional", "Institucional", [PRIVACIDADE, PLANO_MAD, ENTREGA, FALE_CONOSCO, BLOG, TROCAS]),
)

LOOKUP = """{ menus(first: 100) { nodes { id handle title } }
  collections(first: 250) { nodes { id handle } } }"""
CREATE = """mutation($title: String!, $handle: String!, $items: [MenuItemCreateInput!]!) {
  menuCreate(title: $title, handle: $handle, items: $items) { menu { id handle } userErrors { field message code } } }"""
UPDATE = """mutation($id: ID!, $title: String!, $handle: String, $items: [MenuItemUpdateInput!]!) {
  menuUpdate(id: $id, title: $title, handle: $handle, items: $items) { menu { id handle } userErrors { field message code } } }"""


def resolve(items: list, collections: dict) -> list:
    out = []
    for item in items:
        entry = {"title": item["title"], "type": item["type"], "items": resolve(item["items"], collections)}
        if item["type"] == "COLLECTION":
            gid = collections.get(item["handle"])
            if gid:
                entry["resourceId"] = gid
            else:
                entry["type"] = "HTTP"
                entry["url"] = f"/collections/{item['handle']}"
        else:
            entry["url"] = item["url"]
        out.append(entry)
    return out


def sync(client, *, dry_run: bool = False, log=print) -> dict:
    data = client.execute(LOOKUP)
    existing = {menu["handle"]: menu for menu in data["menus"]["nodes"]}
    collections = {node["handle"]: node["id"] for node in data["collections"]["nodes"]}
    stats = {"criados": 0, "atualizados": 0, "erros": 0}
    for handle, title, items in MENUS:
        resolved = resolve(items, collections)
        menu = existing.get(handle)
        if dry_run:
            log(f"  [dry-run] {'atualizaria' if menu else 'criaria'} menu {handle} ({len(resolved)} itens)")
            continue
        if menu:
            result = client.execute(UPDATE, {"id": menu["id"], "title": title, "handle": handle, "items": resolved})["menuUpdate"]
        else:
            result = client.execute(CREATE, {"title": title, "handle": handle, "items": resolved})["menuCreate"]
        if result["userErrors"]:
            stats["erros"] += 1
            log(f"  erro no menu {handle}: {result['userErrors']}")
        else:
            stats["atualizados" if menu else "criados"] += 1
            log(f"  menu {handle}: ok")
    return stats
