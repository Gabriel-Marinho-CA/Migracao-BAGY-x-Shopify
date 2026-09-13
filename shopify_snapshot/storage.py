"""Snapshot da loja Shopify de destino: data/shopify_existing.sqlite.

Um registro por objeto lido (produto, cliente, pedido...), ja normalizado so com
os campos usados no diff, mais o estado de cada recurso (ok, sem permissao,
erro). Uma leitura que falha nao apaga a anterior, mas o diff so confia em
recurso com estado ok.

CONTEM DADOS PESSOAIS (e-mail e telefone de clientes). data/ esta no .gitignore.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    store_domain TEXT,
    api_version  TEXT,
    resources    TEXT,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS records (
    resource TEXT NOT NULL,
    gid      TEXT NOT NULL,
    data     TEXT NOT NULL,
    run_id   INTEGER NOT NULL,
    PRIMARY KEY (resource, gid)
);

CREATE TABLE IF NOT EXISTS resource_state (
    resource    TEXT PRIMARY KEY,
    status      TEXT NOT NULL,      -- running | ok | forbidden | error
    method      TEXT,
    count       INTEGER,
    error       TEXT,
    bulk_id     TEXT,
    started_at  TEXT,
    finished_at TEXT,
    run_id      INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class SnapshotStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- runs

    def start_run(self, store_domain: str, api_version: str, resources: list) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (started_at, store_domain, api_version, resources) VALUES (?, ?, ?, ?)",
                (_now(), store_domain, api_version, _json(resources)))
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                              (_now(), _json(summary), run_id))

    # ----------------------------------------------------------- recursos

    def begin(self, resource: str, method: str, run_id: int) -> None:
        # bulk_id fica: se a rodada anterior caiu no meio, a exportacao e retomada.
        with self.conn:
            self.conn.execute(
                "INSERT INTO resource_state (resource, status, method, started_at, run_id) "
                "VALUES (?, 'running', ?, ?, ?) ON CONFLICT(resource) DO UPDATE SET "
                "status = 'running', method = excluded.method, error = NULL, "
                "started_at = excluded.started_at, run_id = excluded.run_id",
                (resource, method, _now(), run_id))

    def set_bulk_id(self, resource: str, bulk_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE resource_state SET bulk_id = ? WHERE resource = ?", (bulk_id, resource))

    def fail(self, resource: str, status: str, error: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO resource_state (resource, status, error, finished_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(resource) DO UPDATE SET status = excluded.status, error = excluded.error, "
                "finished_at = excluded.finished_at",
                (resource, status, error, _now()))

    def replace(self, resource: str, records: list, run_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM records WHERE resource = ?", (resource,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO records (resource, gid, data, run_id) VALUES (?, ?, ?, ?)",
                [(resource, record["id"], _json(record), run_id) for record in records if record.get("id")])
            self.conn.execute(
                "INSERT INTO resource_state (resource, status, count, finished_at, run_id) "
                "VALUES (?, 'ok', ?, ?, ?) ON CONFLICT(resource) DO UPDATE SET status = 'ok', "
                "count = excluded.count, error = NULL, bulk_id = NULL, "
                "finished_at = excluded.finished_at, run_id = excluded.run_id",
                (resource, len(records), _now(), run_id))

    def state(self, resource: str) -> dict | None:
        return self.states().get(resource)

    def states(self) -> dict:
        cursor = self.conn.execute(
            "SELECT resource, status, method, count, error, bulk_id, started_at, finished_at FROM resource_state")
        names = [column[0] for column in cursor.description]
        return {row[0]: dict(zip(names, row)) for row in cursor.fetchall()}

    def records(self, resource: str) -> list:
        return [json.loads(data) for (data,) in self.conn.execute(
            "SELECT data FROM records WHERE resource = ? ORDER BY rowid", (resource,))]

    # --------------------------------------------------------------- meta

    def set_meta(self, key: str, value) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value = excluded.value, updated_at = excluded.updated_at",
                (key, _json(value), _now()))

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
