"""Imagens do CDN da Bagy dentro do conteudo ja carregado (artigos e paginas).

O texto aponta para cdn.dooca.store / api4.dooca.store, que sai do ar quando a
Bagy for desligada. Para cada item ja carregado:

1. le o texto ATUAL na loja (nao o payload: respeita o que foi editado no admin);
2. sobe cada imagem da Bagy para o Files da Shopify (fileCreate) e espera READY;
3. troca o endereco no texto e grava (articleUpdate / pageUpdate).

Cada imagem enviada fica em load_assets: rodar de novo nao sobe a mesma imagem
duas vezes, e item sem imagem da Bagy no texto e pulado.
"""

from __future__ import annotations

import html
import re
import time
from collections import Counter

import requests

from bagy2shopify.shopify_client import ShopifyError, ShopifyUserError

BAGY_URL = re.compile(r"https?://(?:cdn|api4)\.dooca\.store/[^\s\"'<>)]+")


def probe_image(url: str) -> tuple:
    """(existe, motivo). Baixa o comeco do arquivo SEM token: a Bagy apaga arquivos
    antigos e devolve 404 com JSON, que a Shopify recebe e nao consegue processar."""
    try:
        response = requests.get(url, timeout=30, stream=True, allow_redirects=True)
        kind = response.headers.get("Content-Type") or ""
        head = next(response.iter_content(16), b"")
        response.close()
    except requests.RequestException as exc:
        return False, f"falha ao baixar da Bagy ({exc.__class__.__name__})"
    if response.status_code != 200:
        return False, f"imagem nao existe mais na Bagy (HTTP {response.status_code})"
    if not kind.startswith("image/") or not head:
        return False, f"a Bagy nao devolveu uma imagem ({kind or 'sem tipo'})"
    return True, None

READ = {
    "article": """query ArticleBody($id: ID!) {
  article(id: $id) {
    id
    title
    body
  }
}""",
    "page": """query PageBody($id: ID!) {
  page(id: $id) {
    id
    title
    body
  }
}""",
}

# entidade -> (campo raiz da mutation, argumento do input, documento)
UPDATE = {
    "article": ("articleUpdate", "article", """mutation ArticleBodyUpdate($id: ID!, $article: ArticleUpdateInput!) {
  articleUpdate(id: $id, article: $article) {
    article {
      id
    }
    userErrors {
      field
      message
      code
    }
  }
}"""),
    "page": ("pageUpdate", "page", """mutation PageBodyUpdate($id: ID!, $page: PageUpdateInput!) {
  pageUpdate(id: $id, page: $page) {
    page {
      id
    }
    userErrors {
      field
      message
      code
    }
  }
}"""),
}

FILE_CREATE = """mutation ContentImageCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files {
      id
      fileStatus
    }
    userErrors {
      field
      message
      code
    }
  }
}"""

FILE_STATUS = """query ContentImageStatus($id: ID!) {
  node(id: $id) {
    ... on MediaImage {
      id
      fileStatus
      fileErrors {
        message
      }
      image {
        url
      }
    }
  }
}"""


def short(exc) -> str:
    return " ".join(str(exc).split())[:300]


class ContentImages:
    def __init__(self, client, store, *, dry_run: bool = False, errors=None, log=print,
                 poll_seconds: float = 2.0, wait_seconds: float = 120.0, probe=probe_image):
        self.client = client
        self.store = store
        self.probe = probe
        self.dry_run = dry_run
        self.errors = errors
        self.log = log
        self.poll_seconds = poll_seconds
        self.wait_seconds = wait_seconds
        self.results = Counter()
        self.images = Counter()

    def run(self, entities=("article", "page")) -> Counter:
        items = self.store.items()
        for entity in entities:
            rows = sorted((key, row["shopify_id"]) for (e, key), row in items.items()
                          if e == entity and row.get("status") in ("done", "existing", "partial")
                          and row.get("shopify_id"))
            if not rows:
                continue
            self.log(f"\n[{entity}] {len(rows)} item(ns) carregado(s)")
            for key, gid in rows:
                result, detail = self.one(entity, key, gid)
                self.results[result] += 1
                if result not in ("sem imagem",):
                    self.log(f"  [{result:<12}] {key}" + (f"  {detail}" if detail else ""))
        return self.results

    def _record(self, stage: str, key: str, exc, payload=None) -> None:
        if self.errors is not None:
            self.errors.record(script="load", stage=stage, bagy_id=key, message=short(exc), error=exc,
                               payload=payload, context={"modo": "content-images"})

    def one(self, entity: str, key: str, gid: str) -> tuple:
        try:
            node = self.client.execute(READ[entity], {"id": gid}).get(entity)
        except ShopifyError as exc:
            if exc.status == 401:
                raise
            self._record(f"{entity}-read", key, exc)
            return "falhou", short(exc)
        if not node:
            return "falhou", "item nao encontrado na loja"
        body = node.get("body") or ""
        urls = sorted(set(BAGY_URL.findall(html.unescape(body))))
        if not urls:
            return "sem imagem", None
        if self.dry_run:
            pending = sum(1 for url in urls if (self.store.asset(url) or {}).get("status") != "ready")
            return "enviaria", f"{len(urls)} imagem(ns) da Bagy no texto ({pending} a subir)"

        mapping, failed = {}, []
        for url in urls:
            new = self._shopify_url(url, node.get("title"), key)
            if new:
                mapping[url] = new
            else:
                failed.append(url)
        new_body = body
        for old, new in mapping.items():
            new_body = new_body.replace(html.escape(old, quote=True), new).replace(old, new)
        if new_body == body:
            reasons = sorted({(self.store.asset(url) or {}).get("error") or "?" for url in failed})
            return "falhou", f"{len(failed)} imagem(ns) nao subiram - texto mantido ({'; '.join(reasons)[:160]})"

        root, argument, document = UPDATE[entity]
        variables = {"id": gid, argument: {"body": new_body}}
        try:
            data = self.client.execute(document, variables)
        except ShopifyError as exc:
            if exc.status == 401:
                raise
            self._record(root, key, exc)
            return "falhou", short(exc)
        user_errors = (data.get(root) or {}).get("userErrors") or []
        if user_errors:
            exc = ShopifyUserError(user_errors, request_id=self.client.last_request_id)
            self._record(root, key, exc)
            return "falhou", short(exc)
        detail = f"{len(mapping)} imagem(ns) trocada(s)"
        if failed:
            detail += f"; {len(failed)} nao subiram (ficam apontando para a Bagy)"
        return "corrigido", detail

    def _shopify_url(self, url: str, alt, key: str) -> str | None:
        """URL no CDN da Shopify para a imagem da Bagy (sobe se ainda nao subiu)."""
        asset = self.store.asset(url) or {}
        if asset.get("status") == "ready" and asset.get("cdn_url"):
            self.images["reaproveitada"] += 1
            return asset["cdn_url"]
        # So retoma a espera de um envio em andamento; o que falhou e enviado de novo.
        gid = asset.get("shopify_id") if asset.get("status") == "processing" else None
        if not gid:
            available, reason = self.probe(url)
            if not available:
                self.store.save_asset(url, "failed", shopify_id=None, error=reason)
                self.images["nao existe na Bagy"] += 1
                return None
            files = [{"originalSource": url, "contentType": "IMAGE", "alt": str(alt or "")[:512]}]
            try:
                data = self.client.execute(FILE_CREATE, {"files": files})
            except ShopifyError as exc:
                if exc.status == 401:
                    raise
                self._record("fileCreate", key, exc, files)
                self.store.save_asset(url, "failed", error=short(exc))
                return None
            result = data.get("fileCreate") or {}
            if result.get("userErrors") or not result.get("files"):
                exc = ShopifyUserError(result.get("userErrors") or [], request_id=self.client.last_request_id)
                self._record("fileCreate", key, exc, files)
                self.store.save_asset(url, "failed", error=short(exc))
                return None
            gid = result["files"][0]["id"]
            self.store.save_asset(url, "processing", shopify_id=gid)
            self.images["enviada"] += 1

        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                node = self.client.execute(FILE_STATUS, {"id": gid}).get("node") or {}
            except ShopifyError as exc:
                if exc.status == 401:
                    raise
                node = {}
            status = node.get("fileStatus")
            if status == "READY" and (node.get("image") or {}).get("url"):
                self.store.save_asset(url, "ready", shopify_id=gid, cdn_url=node["image"]["url"], error=None)
                return node["image"]["url"]
            if status == "FAILED":
                reason = "; ".join(e.get("message") or "" for e in node.get("fileErrors") or []) or "FAILED"
                self.store.save_asset(url, "failed", shopify_id=gid, error=reason[:300])
                return None
            if time.monotonic() >= deadline:
                # Continua processando: a proxima rodada espera o mesmo arquivo, sem reenviar.
                return None
            time.sleep(self.poll_seconds)
