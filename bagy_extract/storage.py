"""Armazenamento local da extracao em SQLite.

Uma tabela generica `records` guarda o JSON de cada registro exatamente como
veio da API, com a chave de origem e um hash do conteudo. Com isso:

* rodar de novo e idempotente (upsert pela chave);
* da para saber o que mudou entre rodadas (hash);
* da para saber o que sumiu da Bagy (registro nao visto na ultima passada);
* a fase de transformacao consulta tudo com SQL, sem depender da API.

`resource_state` guarda o checkpoint de paginacao: uma rodada interrompida
retoma da pagina onde parou. Views `v_<recurso>` expoem cada recurso separado.

CONTEM DADOS PESSOAIS (CPF, e-mail, telefone, endereco). Nunca versionar.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    args        TEXT
);

CREATE TABLE IF NOT EXISTS records (
    resource      TEXT NOT NULL,
    record_key    TEXT NOT NULL,
    source_id     TEXT,
    updated_at    TEXT,
    payload       TEXT NOT NULL,
    hash          TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_run_id   INTEGER NOT NULL,
    PRIMARY KEY (resource, record_key)
);
CREATE INDEX IF NOT EXISTS idx_records_seen ON records (resource, last_seen_at);

CREATE TABLE IF NOT EXISTS resource_state (
    resource    TEXT PRIMARY KEY,
    run_id      INTEGER NOT NULL,
    status      TEXT NOT NULL,
    next_page   INTEGER NOT NULL DEFAULT 1,
    last_page   INTEGER,
    api_total   INTEGER,
    fetched     INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS assets (
    url           TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    resources     TEXT,
    local_path    TEXT,
    content_type  TEXT,
    size_bytes    INTEGER,
    sha256        TEXT,
    error         TEXT,
    downloaded_at TEXT
);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def ensure_views(self, names) -> None:
        # Nomes vem do catalogo (identificadores fixos), nao de entrada do usuario.
        with self.conn:
            for name in names:
                self.conn.execute(
                    f'CREATE VIEW IF NOT EXISTS "v_{name}" AS '
                    "SELECT record_key, source_id, updated_at, payload, first_seen_at, last_seen_at "
                    f"FROM records WHERE resource = '{name}'"
                )

    # ----------------------------------------------------------------- runs

    def start_run(self, args: dict) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (started_at, status, args) VALUES (?, 'running', ?)",
                (now_iso(), json.dumps(args, ensure_ascii=False, default=str)),
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
                              (now_iso(), status, run_id))

    # ------------------------------------------------------- estado/checkpoint

    def get_state(self, resource: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM resource_state WHERE resource = ?",
                                (resource,)).fetchone()
        return dict(row) if row else None

    def states(self) -> dict:
        return {row["resource"]: dict(row)
                for row in self.conn.execute("SELECT * FROM resource_state")}

    def begin_resource(self, resource: str, run_id: int, *, fresh: bool) -> int:
        """Abre uma passada no recurso e devolve a pagina inicial.

        Passada interrompida (status running/error com pagina > 1) retoma do
        checkpoint. Passada concluida recomeca do zero, para atualizar tudo.
        """
        state = self.get_state(resource)
        resumable = bool(state and state["status"] in ("running", "error")
                         and state["next_page"] > 1)
        with self.conn:
            if resumable and not fresh:
                self.conn.execute(
                    "UPDATE resource_state SET run_id = ?, status = 'running', error = NULL "
                    "WHERE resource = ?", (run_id, resource))
                return int(state["next_page"])

            self.conn.execute(
                "INSERT INTO resource_state (resource, run_id, status, next_page, started_at) "
                "VALUES (?, ?, 'running', 1, ?) "
                "ON CONFLICT(resource) DO UPDATE SET run_id = excluded.run_id, "
                "status = 'running', next_page = 1, last_page = NULL, api_total = NULL, "
                "fetched = 0, inserted = 0, updated = 0, unchanged = 0, requests = 0, "
                "error = NULL, started_at = excluded.started_at, finished_at = NULL",
                (resource, run_id, now_iso()),
            )
        return 1

    def save_page(self, resource: str, rows: list, *, page_number: int,
                  last_page: int, api_total) -> tuple:
        """Grava uma pagina inteira e avanca o checkpoint, numa unica transacao.

        rows: lista de (record_key, payload). Devolve (novos, alterados, iguais).
        """
        seen_at = now_iso()
        state = self.get_state(resource)
        run_id = state["run_id"] if state else 0
        inserted = updated = unchanged = 0

        with self.conn:
            for key, payload in rows:
                text = canonical_json(payload)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                source_id = payload.get("id") if isinstance(payload, dict) else None
                source_id = None if source_id is None else str(source_id)
                changed_at = ((payload.get("updated_at") or payload.get("created_at"))
                              if isinstance(payload, dict) else None)

                existing = self.conn.execute(
                    "SELECT hash FROM records WHERE resource = ? AND record_key = ?",
                    (resource, key)).fetchone()

                if existing is None:
                    self.conn.execute(
                        "INSERT INTO records (resource, record_key, source_id, updated_at, "
                        "payload, hash, first_seen_at, last_seen_at, last_run_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (resource, key, source_id, changed_at, text, digest,
                         seen_at, seen_at, run_id))
                    inserted += 1
                elif existing["hash"] == digest:
                    self.conn.execute(
                        "UPDATE records SET last_seen_at = ?, last_run_id = ? "
                        "WHERE resource = ? AND record_key = ?",
                        (seen_at, run_id, resource, key))
                    unchanged += 1
                else:
                    self.conn.execute(
                        "UPDATE records SET source_id = ?, updated_at = ?, payload = ?, "
                        "hash = ?, last_seen_at = ?, last_run_id = ? "
                        "WHERE resource = ? AND record_key = ?",
                        (source_id, changed_at, text, digest, seen_at, run_id,
                         resource, key))
                    updated += 1

            self.conn.execute(
                "UPDATE resource_state SET next_page = ?, last_page = ?, api_total = ?, "
                "fetched = fetched + ?, inserted = inserted + ?, updated = updated + ?, "
                "unchanged = unchanged + ?, requests = requests + 1 WHERE resource = ?",
                (page_number + 1, last_page, api_total, len(rows), inserted, updated,
                 unchanged, resource))

        return inserted, updated, unchanged

    def finish_resource(self, resource: str, status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE resource_state SET status = ?, error = ?, finished_at = ? "
                "WHERE resource = ?", (status, error, now_iso(), resource))

    # ------------------------------------------------------------- leitura

    def count(self, resource: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM records WHERE resource = ?",
                                     (resource,)).fetchone()[0])

    def count_seen_since(self, resource: str, since: str | None) -> int:
        if not since:
            return 0
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM records WHERE resource = ? AND last_seen_at >= ?",
            (resource, since)).fetchone()[0])

    def resources_in_db(self) -> list:
        return [row[0] for row in self.conn.execute(
            "SELECT DISTINCT resource FROM records ORDER BY resource")]

    def iter_payloads(self, resource: str):
        """JSON de cada registro, na ordem em que foram vistos pela primeira vez."""
        for row in self.conn.execute(
                "SELECT payload FROM records WHERE resource = ? ORDER BY rowid", (resource,)):
            yield row["payload"]

    def iter_all(self):
        for row in self.conn.execute("SELECT resource, payload FROM records ORDER BY rowid"):
            yield row["resource"], row["payload"]

    # -------------------------------------------------------------- assets

    def register_assets(self, found: dict) -> int:
        """found: {url: {recursos}}. Devolve quantas URLs sao novas."""
        new = 0
        with self.conn:
            for url, resources in found.items():
                row = self.conn.execute("SELECT resources FROM assets WHERE url = ?",
                                        (url,)).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO assets (url, status, resources) VALUES (?, 'pending', ?)",
                        (url, ",".join(sorted(resources))))
                    new += 1
                else:
                    merged = set(filter(None, (row["resources"] or "").split(","))) | set(resources)
                    self.conn.execute("UPDATE assets SET resources = ? WHERE url = ?",
                                      (",".join(sorted(merged)), url))
        return new

    def assets_by_status(self, *statuses) -> list:
        marks = ",".join("?" for _ in statuses)
        return [dict(row) for row in self.conn.execute(
            f"SELECT * FROM assets WHERE status IN ({marks}) ORDER BY url", statuses)]

    def mark_asset(self, url: str, *, status: str, local_path: str | None = None,
                   content_type: str | None = None, size_bytes: int | None = None,
                   sha256: str | None = None, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE assets SET status = ?, local_path = ?, content_type = ?, "
                "size_bytes = ?, sha256 = ?, error = ?, downloaded_at = ? WHERE url = ?",
                (status, local_path, content_type, size_bytes, sha256, error,
                 now_iso() if status == "done" else None, url))

    def asset_summary(self) -> dict:
        return {row["status"]: (row["n"], row["bytes"] or 0) for row in self.conn.execute(
            "SELECT status, COUNT(*) AS n, SUM(size_bytes) AS bytes FROM assets GROUP BY status")}
