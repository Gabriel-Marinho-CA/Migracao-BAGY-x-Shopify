"""Le e resume o log de erros da API.

Uso:
    python tools/show_errors.py                 # resumo + ultimos erros
    python tools/show_errors.py --all           # todos
    python tools/show_errors.py --order 1167    # so de um pedido
    python tools/show_errors.py --stage orderCreate
    python tools/show_errors.py --payload 1167  # imprime o payload que falhou
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy2shopify.config import get_settings


def load(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            print(f"[aviso] linha {line_number} ilegivel, ignorada")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume o log de erros da API.")
    parser.add_argument("--all", action="store_true", help="mostra todos os erros")
    parser.add_argument("--order", metavar="ID", help="filtra por pedido da Bagy")
    parser.add_argument("--stage", help="filtra por etapa (orderCreate, transform, ...)")
    parser.add_argument("--payload", metavar="ID",
                        help="imprime o payload que falhou para esse pedido")
    parser.add_argument("--limit", type=int, default=10,
                        help="quantos erros detalhar (padrao 10)")
    args = parser.parse_args()

    settings = get_settings()
    path = settings.log_dir / "errors.jsonl"
    entries = load(path)

    if not entries:
        print(f"Nenhum erro registrado ({path}).")
        return 0

    if args.order:
        entries = [e for e in entries if str(e.get("bagy_id")) == str(args.order)]
    if args.stage:
        entries = [e for e in entries if e.get("stage") == args.stage]

    if args.payload:
        for entry in reversed(entries):
            if str(entry.get("bagy_id")) == str(args.payload) and entry.get("payload"):
                print(json.dumps(entry["payload"], ensure_ascii=False, indent=2))
                return 0
        print(f"Nenhum payload gravado para o pedido {args.payload}.")
        return 1

    print(f"Arquivo : {path}")
    print(f"Erros   : {len(entries)}\n")

    by_stage = Counter(e.get("stage", "?") for e in entries)
    by_type = Counter(e.get("error_type", "?") for e in entries)
    by_order = Counter(str(e.get("bagy_id")) for e in entries if e.get("bagy_id"))

    print("Por etapa:")
    for stage, count in by_stage.most_common():
        print(f"  {stage:<24} {count}")
    print("\nPor tipo:")
    for kind, count in by_type.most_common():
        print(f"  {kind:<24} {count}")
    if by_order:
        print("\nPedidos com mais erros:")
        for bagy_id, count in by_order.most_common(5):
            print(f"  Bagy #{bagy_id:<18} {count}")

    shown = entries if args.all else entries[-args.limit:]
    print(f"\n{'-' * 72}")
    print(f"Detalhe ({len(shown)} de {len(entries)}):\n")

    for entry in shown:
        head = f"[{entry.get('ts')}] {entry.get('script')}/{entry.get('stage')}"
        if entry.get("bagy_id"):
            head += f"  pedido Bagy #{entry['bagy_id']}"
        print(head)
        print(f"  {entry.get('error_type', 'Erro')}: {entry.get('message', '')[:300]}")
        if entry.get("request_id"):
            print(f"  X-Request-Id: {entry['request_id']}")
        if entry.get("status"):
            print(f"  HTTP: {entry['status']}")
        for user_error in entry.get("user_errors") or []:
            field = ".".join(str(p) for p in (user_error.get("field") or []))
            print(f"  userError {field or '(sem campo)'}: {user_error.get('message')}")
        if entry.get("payload"):
            print(f"  payload gravado (veja com --payload {entry.get('bagy_id')})")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
