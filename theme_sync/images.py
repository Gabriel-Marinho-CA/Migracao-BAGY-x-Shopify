"""Imagens do tema no Files da Shopify.

URLs da Bagy: a Shopify baixa do cdn.dooca.store (fileCreate com originalSource).
Arquivos locais (theme_sync/static): staged upload. A Shopify converte png/jpg
para webp e o nome muda (mad-cor.png -> mad-cor.webp), por isso o nome final
vem da URL da imagem pronta e fica em state/theme_images.json. Antes de subir,
procura um arquivo com o mesmo nome para nao duplicar o que ja esta na loja.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

FIND = """query($q: String!) { files(first: 50, query: $q) {
  nodes { id fileStatus ... on MediaImage { image { url } } } } }"""
CREATE = """mutation($files: [FileCreateInput!]!) { fileCreate(files: $files) {
  files { id fileStatus } userErrors { field code message } } }"""
STATUS = """query($ids: [ID!]!) { nodes(ids: $ids) {
  id ... on MediaImage { fileStatus image { url } fileErrors { code message } } } }"""
STAGE = """mutation($input: [StagedUploadInput!]!) { stagedUploadsCreate(input: $input) {
  stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }"""
BATCH = 10


def key_for(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def stem(url: str) -> str:
    return PurePosixPath(urlparse(url).path).stem.lower()


class ImageLibrary:
    def __init__(self, client, state_path: Path, *, dry_run: bool = False, log=print):
        self.client = client
        self.state_path = state_path
        self.dry_run = dry_run
        self.log = log
        self.state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        self.requested: dict[str, str] = {}

    # ----------------------------------------------------------------- refs

    def ref(self, source: str | Path | None) -> str | None:
        """shopify://shop_images/<nome> da imagem, ou None se ainda nao subiu."""
        if not source:
            return None
        key = self._key(source)
        self.requested.setdefault(key, str(source))
        entry = self.state.get(key)
        return f"shopify://shop_images/{entry['filename']}" if entry else None

    def url(self, source: str | None) -> str | None:
        """URL do CDN da Shopify (para HTML de pagina/secao), ou None se ainda nao subiu."""
        if not source:
            return None
        key = self._key(source)
        self.requested.setdefault(key, str(source))
        entry = self.state.get(key)
        return entry.get("url") if entry else None

    def fill_urls(self) -> None:
        """Completa a URL das imagens gravadas antes de o estado guardar URL."""
        missing = [(key, entry) for key, entry in self.state.items() if not entry.get("url")]
        for start in range(0, len(missing), 50):
            batch = missing[start:start + 50]
            nodes = self.client.execute(STATUS, {"ids": [entry["id"] for _, entry in batch]})["nodes"]
            by_id = {node["id"]: node for node in nodes if node}
            for _, entry in batch:
                url = ((by_id.get(entry["id"]) or {}).get("image") or {}).get("url")
                if url:
                    entry["url"] = url
        if missing:
            self._save()

    def _key(self, source) -> str:
        if isinstance(source, Path):
            digest = hashlib.sha1(source.read_bytes()).hexdigest()[:12]
            return f"local:{source.name}:{digest}"
        return key_for(source)

    # --------------------------------------------------------------- upload

    def ensure(self) -> dict:
        self.fill_urls()
        pending = {key: source for key, source in self.requested.items() if key not in self.state}
        stats = {"na loja": len(self.requested) - len(pending), "reaproveitadas": 0, "enviadas": 0, "falhas": 0}
        if not pending:
            return stats
        remote = {}
        for key, source in list(pending.items()):
            if key.startswith("local:"):
                continue
            found = self._find(source)
            if found:
                self._remember(key, source, found)
                stats["reaproveitadas"] += 1
                del pending[key]
        if self.dry_run:
            for source in pending.values():
                self.log(f"  [dry-run] subiria {source}")
            return stats
        items = list(pending.items())
        for start in range(0, len(items), BATCH):
            batch = items[start:start + BATCH]
            inputs = []
            for key, source in batch:
                original = self._stage(Path(source)) if key.startswith("local:") else source
                inputs.append({"originalSource": original, "contentType": "IMAGE",
                               "alt": PurePosixPath(urlparse(str(source)).path).stem.replace("-", " ")[:500]})
            result = self.client.execute(CREATE, {"files": inputs})["fileCreate"]
            for error in result["userErrors"]:
                self.log(f"  erro no fileCreate: {error.get('message')} ({error.get('field')})")
            ids = {}
            for (key, source), node in zip(batch, result["files"] or []):
                if node and node.get("id"):
                    ids[node["id"]] = (key, source)
            for gid, node in self._wait(list(ids)).items():
                key, source = ids[gid]
                url = ((node or {}).get("image") or {}).get("url")
                if node and node.get("fileStatus") == "READY" and url:
                    self._remember(key, source, {"id": gid, "url": url})
                    stats["enviadas"] += 1
                else:
                    stats["falhas"] += 1
                    self.log(f"  imagem nao ficou pronta: {source} {(node or {}).get('fileErrors')}")
            self._save()
        return stats

    def _find(self, source: str) -> dict | None:
        name = stem(source)
        nodes = self.client.execute(FIND, {"q": f"filename:{name}"})["files"]["nodes"]
        for node in nodes:
            url = ((node.get("image") or {}).get("url")) or ""
            if url and stem(url) == name and node.get("fileStatus") == "READY":
                return {"id": node["id"], "url": url}
        return None

    def _stage(self, path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        target = self.client.execute(STAGE, {"input": [{
            "filename": path.name, "mimeType": mime, "httpMethod": "POST", "resource": "IMAGE",
            "fileSize": str(path.stat().st_size)}]})["stagedUploadsCreate"]
        if target["userErrors"]:
            raise RuntimeError(f"stagedUploadsCreate: {target['userErrors']}")
        staged = target["stagedTargets"][0]
        fields = {param["name"]: param["value"] for param in staged["parameters"]}
        with path.open("rb") as handle:
            response = requests.post(staged["url"], data=fields, files={"file": (path.name, handle, mime)}, timeout=120)
        response.raise_for_status()
        return staged["resourceUrl"]

    def _wait(self, ids: list, timeout: int = 90) -> dict:
        nodes = {}
        deadline = time.monotonic() + timeout
        while ids and time.monotonic() < deadline:
            time.sleep(2)
            for node in self.client.execute(STATUS, {"ids": ids})["nodes"]:
                if node and node.get("fileStatus") in ("READY", "FAILED"):
                    nodes[node["id"]] = node
            ids = [gid for gid in ids if gid not in nodes]
        for gid in ids:
            nodes[gid] = None
        return nodes

    def _remember(self, key: str, source, found: dict) -> None:
        self.state[key] = {"id": found["id"], "filename": PurePosixPath(urlparse(found["url"]).path).name,
                           "url": found["url"], "source": str(source)}
        self._save()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")
