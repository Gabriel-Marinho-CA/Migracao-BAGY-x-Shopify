"""Leitura dos dados extraidos (somente leitura sobre data/bagy.sqlite)."""

from __future__ import annotations

import json
import sqlite3
from functools import cached_property
from pathlib import Path


class Source:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"Banco da extracao nao encontrado: {self.path}. "
                "Rode primeiro: python run_extraction.py")
        self.conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        self._cache: dict = {}

    def close(self) -> None:
        self.conn.close()

    def all(self, resource: str) -> list:
        if resource not in self._cache:
            self._cache[resource] = [json.loads(row[0]) for row in self.conn.execute(
                "SELECT payload FROM records WHERE resource = ? ORDER BY rowid", (resource,))]
        return self._cache[resource]

    def one(self, resource: str):
        rows = self.all(resource)
        return rows[0] if rows else None

    @cached_property
    def settings(self) -> dict:
        value = self.one("settings")
        return value if isinstance(value, dict) else {}

    @cached_property
    def shop_id(self):
        return self.settings.get("id")

    @cached_property
    def store_hosts(self) -> set:
        """Dominios da loja (proprio e o *.bagypro.com), para reescrever links internos."""
        hosts = set()
        for key in ("domain", "dooca_domain"):
            host = (self.settings.get(key) or "").strip().lower()
            if host:
                hosts.add(host)
                hosts.add(host[4:] if host.startswith("www.") else "www." + host)
        return hosts

    def extracted_at(self) -> str | None:
        return self.conn.execute("SELECT MAX(last_seen_at) FROM records").fetchone()[0]

    def extraction_states(self) -> dict:
        try:
            return {row[0]: row[1] for row in self.conn.execute(
                "SELECT resource, status FROM resource_state")}
        except sqlite3.Error:
            return {}
