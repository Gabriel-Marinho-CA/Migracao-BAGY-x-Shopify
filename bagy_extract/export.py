"""Exporta o SQLite para JSON: um arquivo por recurso, um registro por linha.

O SQLite continua sendo a fonte da verdade; o export existe para inspecao
rapida (abrir no editor, grep, jq) e para quem nao quiser consultar SQL.
"""

from __future__ import annotations

import json
from pathlib import Path

from .storage import now_iso


def export_json(storage, export_dir: Path, names=None, *, log=print) -> list:
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(names) if names else None
    states = storage.states()
    manifest = {"exported_at": now_iso(), "resources": {}}
    written = []

    for name in storage.resources_in_db():
        if wanted is not None and name not in wanted:
            continue
        path = export_dir / f"{name}.json"
        count = 0
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("[\n")
            for payload in storage.iter_payloads(name):
                if count:
                    handle.write(",\n")
                handle.write(payload)
                count += 1
            handle.write("\n]\n")

        state = states.get(name) or {}
        manifest["resources"][name] = {
            "file": path.name,
            "records": count,
            "api_total": state.get("api_total"),
            "status": state.get("status"),
            "extracted_at": state.get("finished_at"),
        }
        written.append((name, count, path))

    (export_dir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(count for _, count, _ in written)
    log(f"\nExport JSON: {len(written)} arquivo(s), {total} registro(s) em {export_dir}")
    return written
