"""Envia theme/ para o tema da loja, so o que mudou (checksum MD5 da Shopify).

Templates JSON, grupos de secoes e settings_data tambem sao editados pelo
editor de temas: se o arquivo mudou na loja depois do ultimo envio, ele e
pulado (use --force para sobrescrever). O estado fica em state/theme_push.json.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections import Counter

from .config import STATE_DIR, THEME_DIR

FOLDERS = ("assets", "blocks", "config", "layout", "locales", "sections", "snippets", "templates")
BINARY = {".woff2", ".woff", ".ttf", ".otf", ".eot", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp4", ".pdf"}
BATCH = 10

THEMES = "{ themes(first: 20) { nodes { id name role } } }"
FILES = """query($id: ID!, $after: String) { theme(id: $id) { files(first: 100, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { filename checksumMd5 } } } }"""
FILES_BY_NAME = """query($id: ID!, $names: [String!]) { theme(id: $id) { files(first: 100, filenames: $names) {
  nodes { filename checksumMd5 } } } }"""
UPSERT = """mutation($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) {
  themeFilesUpsert(themeId: $themeId, files: $files) {
    upsertedThemeFiles { filename } job { id } userErrors { filename code message } } }"""
JOB = "query($id: ID!) { job(id: $id) { done } }"


def resolve_theme(client, theme_id: str = "") -> dict:
    """Tema de destino fixo: THEME_ID, ou o gravado em state/theme_target.json.

    Nunca segue o tema publicado: theme/ foi baixado de um tema especifico e
    enviar para outro sobrescreve os arquivos dele.
    """
    themes = client.execute(THEMES)["themes"]["nodes"]
    target_file = STATE_DIR / "theme_target.json"
    recorded = json.loads(target_file.read_text(encoding="utf-8")) if target_file.exists() else {}
    wanted = (theme_id or recorded.get("id") or "").split("/")[-1]
    if not wanted:
        raise SystemExit("ERRO: tema de destino nao definido: defina THEME_ID ou state/theme_target.json")
    for theme in themes:
        if theme["id"].split("/")[-1] == wanted:
            if recorded.get("name") and theme["name"] != recorded["name"] and not theme_id:
                raise SystemExit(f"ERRO: o tema {wanted} agora se chama {theme['name']} (esperado {recorded['name']})")
            return theme
    raise SystemExit(f"ERRO: tema {wanted} nao existe mais na loja")


def editable(name: str) -> bool:
    return (name.startswith("templates/") and name.endswith(".json")) or name in (
        "config/settings_data.json", "sections/header-group.json", "sections/footer-group.json")


def priority(name: str) -> int:
    folder = name.split("/", 1)[0]
    if name == "config/settings_data.json":
        return 6
    if name.startswith("templates/"):
        return 5
    if name.endswith(".json") and folder == "sections":
        return 4
    return {"assets": 0, "snippets": 1, "blocks": 1, "sections": 2}.get(folder, 3)


def local_files() -> list:
    found = []
    for folder in FOLDERS:
        base = THEME_DIR / folder
        if base.exists():
            for path in base.rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    found.append((path.relative_to(THEME_DIR).as_posix(), path))
    return sorted(found, key=lambda item: (priority(item[0]), item[0]))


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def checksums(name: str, data: bytes) -> set:
    """MD5s que a Shopify pode guardar para o arquivo.

    Em JSON o checksum da loja nao e o do texto servido: as vezes e sem o
    comentario do topo, as vezes o JSON compacto.
    """
    found = {md5(data)}
    if name.endswith(".json"):
        try:
            text = re.sub(r"^\s*/\*.*?\*/", "", data.decode("utf-8"), count=1, flags=re.S).lstrip()
            found.add(md5(text.encode("utf-8")))
            compact = json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
            found.add(md5(compact.encode("utf-8")))
        except ValueError:
            pass
    return found


def body(name: str, data: bytes) -> dict:
    if any(name.lower().endswith(ext) for ext in BINARY):
        return {"type": "BASE64", "value": base64.b64encode(data).decode("ascii")}
    return {"type": "TEXT", "value": data.decode("utf-8")}


class Pusher:
    def __init__(self, client, theme: dict, *, force: bool = False, dry_run: bool = False, log=print):
        self.client = client
        self.theme = theme
        self.force = force
        self.dry_run = dry_run
        self.log = log
        self.state_path = STATE_DIR / "theme_push.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        self.known = self.state.setdefault(theme["id"], {})

    def remote(self) -> dict:
        checksums, after = {}, None
        while True:
            files = self.client.execute(FILES, {"id": self.theme["id"], "after": after})["theme"]["files"]
            checksums.update({node["filename"]: node["checksumMd5"] for node in files["nodes"]})
            if not files["pageInfo"]["hasNextPage"]:
                return checksums
            after = files["pageInfo"]["endCursor"]

    def run(self, only: set | None = None) -> Counter:
        stats = Counter()
        remote = self.remote()
        plan = []
        for name, path in local_files():
            if only and name not in only:
                continue
            data = path.read_bytes()
            digest = md5(data)
            remote_md5 = remote.get(name)
            last = self.known.get(name) or {}
            if remote_md5 in checksums(name, data):
                self.known[name] = {"local": digest, "remote": remote_md5}
                stats["iguais"] += 1
            elif remote_md5 and last.get("local") == digest and last.get("remote") == remote_md5:
                stats["iguais"] += 1
            elif remote_md5 and editable(name) and last and last.get("remote") != remote_md5 and not self.force:
                self.log(f"  pulado, editado no admin depois do ultimo envio: {name} (use --force)")
                stats["editados no admin"] += 1
            else:
                plan.append((name, data, digest))
        for start in range(0, len(plan), BATCH):
            batch = plan[start:start + BATCH]
            if self.dry_run:
                for name, _, _ in batch:
                    self.log(f"  [dry-run] enviaria {name}")
                stats["a enviar"] += len(batch)
                continue
            result = self.client.execute(UPSERT, {"themeId": self.theme["id"], "files": [
                {"filename": name, "body": body(name, data)} for name, data, _ in batch]})["themeFilesUpsert"]
            if result.get("job"):
                self._wait(result["job"]["id"])
            failed = set()
            for error in result["userErrors"]:
                failed.add(error.get("filename"))
                self.log(f"  ERRO {error.get('filename')}: {error.get('code')} {error.get('message')}")
            sent = [(name, digest) for name, _, digest in batch if name not in failed]
            if sent:
                nodes = self.client.execute(FILES_BY_NAME, {"id": self.theme["id"], "names": [n for n, _ in sent]})
                stored = {node["filename"]: node["checksumMd5"] for node in nodes["theme"]["files"]["nodes"]}
                for name, digest in sent:
                    self.known[name] = {"local": digest, "remote": stored.get(name)}
                    self.log(f"  enviado {name}")
            stats["enviados"] += len(sent)
            stats["erros"] += len(failed)
            self._save()
        self._save()
        return stats

    def _wait(self, job_id: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.client.execute(JOB, {"id": job_id})["job"]["done"]:
                return
            time.sleep(2)

    def _save(self) -> None:
        if self.dry_run:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
