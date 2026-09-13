"""Backup local das imagens e arquivos hospedados na Bagy.

As imagens de produto, post e hotsite apontam para hosts da Bagy
(cdn.dooca.store, api4.dooca.store). Quando a loja for desligada essas URLs
morrem - e a Shopify precisa buscar as imagens de algum lugar na hora de criar
produtos e posts. Por isso elas sao baixadas agora.

A varredura procura URLs em TODO o JSON extraido, inclusive dentro do HTML de
descricao de produto e de post, filtrando pelos hosts de EXTRACT_ASSET_HOSTS.
Nunca envia o token da Bagy para o CDN.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from .client import Pacer

ASSET_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "svg", "avif", "bmp", "ico",
                    "pdf", "mp4", "webm"}
URL_RE = re.compile(r"https?://[^\s\"'<>()\\]+")
# O editor visual da Bagy grava imagens como media://files/x.png - o mesmo arquivo
# de https://cdn.dooca.store/<id da loja>/files/x.png (confirmado nos dados).
MEDIA_RE = re.compile(r"media://[^\s\"'<>()\\]+")
PERMANENT_FAILURES = {400, 401, 403, 404, 410}


def _shop_id(storage):
    row = storage.conn.execute(
        "SELECT payload FROM records WHERE resource = 'settings' LIMIT 1").fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0]).get("id")
    except (ValueError, AttributeError):
        return None


def collect_urls(storage, hosts) -> dict:
    hosts = {host.lower() for host in hosts}
    shop_id = _shop_id(storage)
    found: dict = {}
    for resource, payload in storage.iter_all():
        text = html.unescape(payload)
        candidates = URL_RE.findall(text)
        if shop_id:
            candidates += [f"https://cdn.dooca.store/{shop_id}/{match[len('media://'):]}"
                           for match in MEDIA_RE.findall(text)]
        for raw in candidates:
            url = raw.rstrip(".,;")
            parsed = urlparse(url)
            if parsed.netloc.lower() not in hosts:
                continue
            filename = parsed.path.rsplit("/", 1)[-1]
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if extension not in ASSET_EXTENSIONS:
                continue
            found.setdefault(url, set()).add(resource)
    return found


def local_path(assets_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    parts = [part for part in unquote(parsed.path).split("/") if part]
    safe = [re.sub(r'[<>:"|?*\\]', "_", part)[:120] for part in parts] or ["index"]
    path = Path(assets_dir).joinpath(parsed.netloc, *safe)
    if parsed.query:
        digest = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:10]
        path = path.with_name(f"{path.stem}_{digest}{path.suffix}")
    return path


def run(storage, settings, *, log=print, max_retries: int = 3) -> int:
    found = collect_urls(storage, settings.asset_hosts)
    new = storage.register_assets(found)
    pending = storage.assets_by_status("pending", "error")

    log(f"\nAssets: {len(found)} URL(s) em {', '.join(settings.asset_hosts)} "
        f"| {new} nova(s) | {len(pending)} a baixar")
    if not pending:
        return 0

    session = requests.Session()
    session.headers["User-Agent"] = "bagy-extract/0.1"
    pacer = Pacer(settings.asset_requests_per_minute)
    ok = failed = 0

    for index, asset in enumerate(pending, 1):
        url = asset["url"]
        target = local_path(settings.assets_dir, url)
        error = None

        for attempt in range(1, max_retries + 1):
            pacer.wait()
            try:
                response = session.get(url, timeout=60)
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    content = response.content
                    target.parent.mkdir(parents=True, exist_ok=True)
                    partial = target.with_name(target.name + ".part")
                    partial.write_bytes(content)
                    partial.replace(target)
                    storage.mark_asset(
                        url, status="done", local_path=str(target),
                        content_type=response.headers.get("Content-Type"),
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                    error = None
                    break
                error = f"HTTP {response.status_code}"
                if response.status_code in PERMANENT_FAILURES:
                    break
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 20))

        if error:
            storage.mark_asset(url, status="error", error=error)
            failed += 1
        else:
            ok += 1

        if index % 25 == 0 or index == len(pending):
            log(f"    {index}/{len(pending)}  ok {ok}  falhas {failed}")

    summary = storage.asset_summary()
    done_count, done_bytes = summary.get("done", (0, 0))
    log(f"    total baixado: {done_count} arquivo(s), {done_bytes / 1_048_576:.1f} MB "
        f"em {settings.assets_dir}")
    return 1 if failed else 0
