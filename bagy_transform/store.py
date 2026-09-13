"""Saida da transformacao: data/shopify.sqlite.

Uma linha por payload, com status, variaveis, pos-acoes, refs que cria e de que
depende. E a entrada da fase de carga. Cada rodada substitui as entidades
transformadas (a transformacao e deterministica e barata de refazer).

CONTEM DADOS PESSOAIS. data/ esta no .gitignore.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    reference_date TEXT,
    entities       TEXT,
    summary        TEXT
);

CREATE TABLE IF NOT EXISTS payloads (
    entity        TEXT NOT NULL,
    source_key    TEXT NOT NULL,
    position      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    skip_reason   TEXT,
    mutation      TEXT,
    variables     TEXT,
    post_actions  TEXT,
    provides      TEXT,
    depends_on    TEXT,
    warnings      TEXT,
    schema_errors TEXT,
    hash          TEXT,
    run_id        INTEGER NOT NULL,
    PRIMARY KEY (entity, source_key)
);
CREATE INDEX IF NOT EXISTS idx_payloads_status ON payloads (entity, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class TransformStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_run(self, reference_date: str, entities: list) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO runs (started_at, reference_date, entities) VALUES (?, ?, ?)",
                (_now(), reference_date, _json(entities)))
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                              (_now(), _json(summary), run_id))

    def replace(self, entity: str, payloads: list, run_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM payloads WHERE entity = ?", (entity,))
            for position, payload in enumerate(payloads):
                body = _json({"mutation": payload.mutation, "variables": payload.variables,
                              "post_actions": payload.post_actions})
                self.conn.execute(
                    "INSERT INTO payloads (entity, source_key, position, status, skip_reason, "
                    "mutation, variables, post_actions, provides, depends_on, warnings, "
                    "schema_errors, hash, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (entity, payload.source_key, position, payload.status, payload.skip_reason,
                     payload.mutation,
                     _json(payload.variables) if payload.mutation else None,
                     _json(payload.post_actions), _json(payload.provides),
                     _json(payload.depends_on), _json(payload.warnings),
                     _json(payload.schema_errors + [f"aviso: {n}" for n in payload.schema_notices]),
                     hashlib.sha256(body.encode("utf-8")).hexdigest(), run_id))
