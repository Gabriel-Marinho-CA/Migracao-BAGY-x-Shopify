"""Imagens dos produtos: a Shopify baixa cada arquivo da URL do CDN da Bagy.

Na carga, o `productSet` ja leva as imagens em `files` (LOAD_PRODUCT_IMAGES=1).
Este modulo cobre o que ficou sem imagem (ex.: o piloto, que subiu sem) com
`productUpdate(media:)`, que ADICIONA midia sem mexer no resto do produto.
Nao usa `productSet` aqui: numa atualizacao ele substitui a lista de arquivos.

Nunca duplica: so envia para produto com ZERO imagens na Shopify. Produto com
todas as imagens e marcado como feito; com parte delas, fica para conferir.
"""

from __future__ import annotations

import time
from collections import Counter

from bagy2shopify.shopify_client import ShopifyError, ShopifyUserError
from bagy_transform.common import compact

MEDIA_ADD = """mutation ProductMediaAdd($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
  productUpdate(product: $product, media: $media) {
    product {
      id
      mediaCount {
        count
      }
    }
    userErrors {
      field
      message
    }
  }
}"""

MEDIA_STATUS = """query ProductMediaStatus($id: ID!) {
  product(id: $id) {
    id
    mediaCount {
      count
    }
    media(first: 50) {
      nodes {
        id
        status
        mediaContentType
        mediaErrors {
          code
          message
        }
      }
    }
  }
}"""

ENTITY = "product_media"
PENDING = ("UPLOADED", "PROCESSING")


def product_files(item) -> list:
    return [f for f in (item.variables.get("input") or {}).get("files") or [] if f.get("originalSource")]


def media_input(files: list) -> list:
    return [compact({"originalSource": f["originalSource"], "alt": f.get("alt"),
                     "mediaContentType": f.get("contentType") or "IMAGE"}) for f in files]


def short(exc) -> str:
    return " ".join(str(exc).split())[:300]


def media_problems(nodes: list) -> tuple:
    """(prontas, recusadas com motivo, ainda processando)."""
    ready = sum(1 for node in nodes if node.get("status") == "READY")
    failed = [node for node in nodes if node.get("status") == "FAILED"]
    pending = sum(1 for node in nodes if node.get("status") in PENDING)
    reason = None
    if failed:
        reasons = sorted({error.get("message") or error.get("code") or "?" for node in failed
                          for error in node.get("mediaErrors") or []})
        reason = f"{len(failed)} imagem(ns) recusada(s) pela Shopify: {'; '.join(reasons)[:200]}"
    return ready, reason, pending


class MediaBackfill:
    def __init__(self, client, store, *, dry_run: bool = False, errors=None, run_id: int | None = None, log=print,
                 poll_seconds: float = 3.0, wait_seconds: float = 90.0):
        self.client = client
        self.store = store
        self.dry_run = dry_run
        self.errors = errors
        self.run_id = run_id
        self.log = log
        self.poll_seconds = poll_seconds
        self.wait_seconds = wait_seconds
        self.results = Counter()
        self.touched = []

    def run(self, products: list) -> Counter:
        self.log(f"\n[imagens] {len(products)} produto(s)")
        for item in products:
            result, detail = self.one(item)
            self.results[result] += 1
            self.touched.append(("product", item.source_key, result, detail))
            self.log(f"  [{result:<12}] {item.source_key}" + (f"  {detail}" if detail else ""))
        return self.results

    def _mark(self, item, status: str, gid: str | None, warnings=None, error=None) -> None:
        if not self.dry_run:
            self.store.mark(ENTITY, item.source_key, status, shopify_id=gid, payload_hash=item.hash,
                            warnings=list(warnings or []), error=error, run_id=self.run_id)

    def _fail(self, item, gid, exc, payload) -> tuple:
        if self.errors is not None:
            self.errors.record(script="load", stage="productUpdate-media", bagy_id=item.source_key, message=short(exc),
                               error=exc, payload=payload, context={"entity": ENTITY})
        self._mark(item, "failed", gid, error=short(exc))
        return "falhou", short(exc)

    def _product(self, gid: str) -> dict:
        return self.client.execute(MEDIA_STATUS, {"id": gid}).get("product") or {}

    def one(self, item) -> tuple:
        files = product_files(item)
        if not files:
            return "sem imagem", None
        state = self.store.items()
        product = state.get(("product", item.source_key)) or {}
        gid = product.get("shopify_id")
        if product.get("status") not in ("done", "existing", "partial") or not gid:
            return "pulado", "produto ainda nao carregado"
        if (state.get((ENTITY, item.source_key)) or {}).get("status") == "done":
            return "ja carregado", None

        try:
            current = self._product(gid)
        except ShopifyError as exc:
            if exc.status == 401:
                raise
            return self._fail(item, gid, exc, None)
        count = int(((current.get("mediaCount") or {}).get("count")) or 0)
        if count >= len(files):
            return self._settle(item, gid, (current.get("media") or {}).get("nodes") or [], len(files), "ja tinha")
        if count:
            reason = f"{count} de {len(files)} imagens na Shopify: conferir no admin (nao reenvia para nao duplicar)"
            self._mark(item, "blocked", gid, error=reason)
            return "incompleto", reason
        if self.dry_run:
            return "enviaria", f"{len(files)} imagem(ns)"

        media = media_input(files)
        self._mark(item, "sending", gid)
        try:
            data = self.client.execute(MEDIA_ADD, {"product": {"id": gid}, "media": media})
        except ShopifyError as exc:
            if exc.status == 401:
                raise
            return self._fail(item, gid, exc, media)
        result = data.get("productUpdate") or {}
        if result.get("userErrors"):
            exc = ShopifyUserError(result["userErrors"], request_id=self.client.last_request_id)
            return self._fail(item, gid, exc, media)

        return self._settle(item, gid, self._wait(gid), len(files), "enviado")

    def _settle(self, item, gid: str, nodes: list, expected: int, label: str) -> tuple:
        """Marca pelo status real das imagens na Shopify.

        Recusada: fica bloqueado para revisar (nao reenvia sozinho, para nao
        duplicar as que deram certo). Processando: a proxima rodada confere de novo.
        """
        ready, failed, pending = media_problems(nodes)
        detail = f"{ready} pronta(s) de {expected}"
        if failed:
            self._mark(item, "blocked", gid, error=failed)
            return "recusada", f"{detail}  {failed}"
        if pending:
            self._mark(item, "partial", gid, error=f"{pending} imagem(ns) ainda processando")
            return "processando", f"{detail}, {pending} processando: rode --images de novo para conferir"
        self._mark(item, "done", gid)
        return label, detail

    def _wait(self, gid: str) -> list:
        """Espera a Shopify baixar e processar as imagens (ou estourar o tempo)."""
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                nodes = (self._product(gid).get("media") or {}).get("nodes") or []
            except ShopifyError:
                return []
            if not any(node.get("status") in PENDING for node in nodes) or time.monotonic() >= deadline:
                return nodes
            time.sleep(self.poll_seconds)
