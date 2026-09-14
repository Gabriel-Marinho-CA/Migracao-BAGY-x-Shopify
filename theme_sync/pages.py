"""Paginas da loja (criadas por run_load.py): imagens da Bagy no HTML viram arquivos da Shopify.

O corpo das paginas vem da transformacao com as imagens apontando para o CDN da
Bagy (e, em alguns hotsites, imagens de exemplo do editor da Bagy). Aqui cada
imagem sobe para o Files da Shopify, a URL e trocada no HTML e as de exemplo
saem. Pagina que ainda nao existe na loja fica para a proxima rodada.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

from .bagy import MEDIA_URL, image_source, rewrite_media
from .config import ROOT

TRANSFORM_DB = ROOT / "data" / "shopify.sqlite"
PAGE = "query($q: String!) { pages(first: 5, query: $q) { nodes { id handle body } } }"
UPDATE = """mutation($id: ID!, $page: PageUpdateInput!) { pageUpdate(id: $id, page: $page) {
  page { id handle } userErrors { field message } } }"""


def handles() -> list:
    with sqlite3.connect(TRANSFORM_DB) as conn:
        rows = conn.execute("select variables from payloads where entity = 'page' and status = 'ready'").fetchall()
    found = []
    for (variables,) in rows:
        data = json.loads(variables or "{}")
        page = next(iter(data.values()), {}) if isinstance(data, dict) else {}
        if isinstance(page, dict) and page.get("handle"):
            found.append(page["handle"])
    return found


def collect(client, images) -> tuple:
    """(paginas {handle: pagina}, handles que ainda nao existem). Pede as imagens do HTML na biblioteca."""
    pages, missing = {}, []
    for handle in handles():
        nodes = client.execute(PAGE, {"q": f"handle:{handle}"})["pages"]["nodes"]
        page = next((node for node in nodes if node["handle"] == handle), None)
        if not page:
            missing.append(handle)
            continue
        for match in MEDIA_URL.finditer(page.get("body") or ""):
            source = image_source(match.group(0))
            if source:
                images.url(source)
        pages[handle] = page
    return pages, missing


def run(client, images, pages: dict, missing: list, *, dry_run: bool = False, log=print) -> Counter:
    stats = Counter({"pagina ainda nao existe": len(missing)}) if missing else Counter()
    for handle, page in sorted(pages.items()):
        body = page.get("body") or ""
        new_body = rewrite_media(body, images)
        if new_body == body:
            stats["ja sem imagens da Bagy"] += 1
            continue
        if MEDIA_URL.search(new_body):
            log(f"  aviso: {handle} ainda tem imagem da Bagy que nao subiu")
        if dry_run:
            log(f"  [dry-run] atualizaria o HTML da pagina {handle}")
            stats["a atualizar"] += 1
            continue
        result = client.execute(UPDATE, {"id": page["id"], "page": {"body": new_body}})["pageUpdate"]
        if result["userErrors"]:
            log(f"  erro na pagina {handle}: {result['userErrors']}")
            stats["erros"] += 1
        else:
            log(f"  pagina {handle}: imagens no Files da Shopify")
            stats["atualizadas"] += 1
    return stats
