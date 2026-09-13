"""Progresso da carga, dentro de data/shopify.sqlite.

* `load_items` - uma linha por payload tocado: status, ID na Shopify, pos-acoes
  ja feitas, avisos e ultimo erro.
    sending    a mutation principal foi disparada e a resposta nao chegou
               (processo caiu no meio): tratado como incerto
    partial    criado; faltam pos-acoes (a proxima rodada continua dali)
    done       criado/atualizado com todas as pos-acoes
    existing   ja existia na loja (diff = skip)
    failed     recusado (userErrors ou erro sem efeito): pode tentar de novo
    uncertain  pode ter sido criado sem a resposta chegar: NAO reenvia ate um
               novo snapshot conferir
    blocked    nao enviado (diff, referencia pendente)
    removed    criado pela carga e apagado depois (run_load.py --cleanup)
* `load_refs` - bagy-ref -> ID criado pela carga. Sobrevive ao diff, que
  refaz `ref_map` a cada rodada.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS load_items (
    entity       TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    status       TEXT NOT NULL,
    shopify_id   TEXT,
    payload_hash TEXT,
    steps        TEXT,
    warnings     TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    run_id       INTEGER,
    PRIMARY KEY (entity, source_key)
);

CREATE TABLE IF NOT EXISTS load_refs (
    ref        TEXT PRIMARY KEY,
    shopify_id TEXT NOT NULL,
    entity     TEXT,
    source_key TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS load_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    store_domain TEXT,
    mode         TEXT,
    summary      TEXT
);
"""

COLUMNS = ("status", "shopify_id", "payload_hash", "steps", "warnings", "error", "attempts", "run_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LoadStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _table_exists(self, name: str) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    # ----------------------------------------------------------------- leitura

    def existing(self) -> dict:
        """Resultado do diff por (entity, source_key)."""
        if not self._table_exists("existing"):
            return {}
        cursor = self.conn.execute(
            "SELECT entity, source_key, action, shopify_id, match_by, payload_hash, diff_at FROM existing")
        return {(row[0], row[1]): {"action": row[2], "shopify_id": row[3], "match_by": row[4],
                                   "payload_hash": row[5], "diff_at": row[6]} for row in cursor}

    def refs(self) -> dict:
        refs = {}
        if self._table_exists("ref_map"):
            refs.update(dict(self.conn.execute("SELECT ref, shopify_id FROM ref_map")))
        refs.update(dict(self.conn.execute("SELECT ref, shopify_id FROM load_refs")))
        return refs

    def items(self) -> dict:
        cursor = self.conn.execute(f"SELECT entity, source_key, {', '.join(COLUMNS)} FROM load_items")
        return {(row[0], row[1]): dict(zip(COLUMNS, row[2:])) for row in cursor}

    # ----------------------------------------------------------------- escrita

    def mark(self, entity: str, source_key: str, status: str, **fields) -> None:
        """Atualiza o estado do item. Campos nao informados ficam como estavam."""
        current = self.items().get((entity, source_key)) or {"attempts": 0}
        row = {**current, **{k: v for k, v in fields.items() if k in COLUMNS}, "status": status}
        for name in ("steps", "warnings"):
            if isinstance(row.get(name), (list, tuple)):
                row[name] = json.dumps(list(row[name]), ensure_ascii=False)
        if fields.get("attempt"):
            row["attempts"] = int(current.get("attempts") or 0) + 1
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO load_items (entity, source_key, status, shopify_id, payload_hash, steps, "
                "warnings, error, attempts, updated_at, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entity, source_key, row["status"], row.get("shopify_id"), row.get("payload_hash"), row.get("steps"),
                 row.get("warnings"), row.get("error"), int(row.get("attempts") or 0), _now(), row.get("run_id")))

    def add_refs(self, mapping: dict, entity: str, source_key: str) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO load_refs (ref, shopify_id, entity, source_key, created_at) VALUES (?, ?, ?, ?, ?)",
                [(ref, gid, entity, source_key, _now()) for ref, gid in mapping.items()])

    def forget_refs(self, entity: str, source_key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM load_refs WHERE entity = ? AND source_key = ?", (entity, source_key))

    def start_run(self, store_domain: str, mode: str) -> int:
        with self.conn:
            cursor = self.conn.execute("INSERT INTO load_runs (started_at, store_domain, mode) VALUES (?, ?, ?)",
                                       (_now(), store_domain, mode))
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE load_runs SET finished_at = ?, summary = ? WHERE id = ?",
                              (_now(), json.dumps(summary, ensure_ascii=False, default=str), run_id))
