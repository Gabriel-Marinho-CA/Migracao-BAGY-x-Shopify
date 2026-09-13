"""Resume o que foi extraido da Bagy, sem imprimir dados pessoais.

Mostra contagens por recurso contra o total da API e agregados que ajudam a
planejar a transformacao: status de pedidos, periodo, meios de pagamento,
cobertura de CPF/CNPJ, variacoes e imagens de produto, assets baixados.

Uso: python tools/inspect_extraction.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bagy_extract.config import get_settings
from bagy_extract.resources import GROUPS, RESOURCES
from bagy_extract.storage import Storage


def load(storage, name: str) -> list:
    return [json.loads(payload) for payload in storage.iter_payloads(name)]


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def top(counter: Counter, n: int = 8) -> str:
    return ", ".join(f"{key}={value}" for key, value in counter.most_common(n)) or "-"


def main() -> int:
    settings = get_settings()
    if not settings.db_path.exists():
        print(f"Banco nao encontrado: {settings.db_path}")
        print("Rode primeiro: python run_extraction.py")
        return 1

    storage = Storage(settings.db_path)
    states = storage.states()
    size_mb = settings.db_path.stat().st_size / 1_048_576
    print(f"Banco: {settings.db_path} ({size_mb:.1f} MB)")

    print(f"\n{'Recurso':<26} {'No banco':>9} {'API':>7}  {'Status':<11} Extraido em")
    for group in GROUPS:
        print(f"[{group}]")
        for resource in (r for r in RESOURCES if r.group == group):
            state = states.get(resource.name) or {}
            api_total = state.get("api_total")
            print(f"  {resource.name:<24} {storage.count(resource.name):>9} "
                  f"{str(api_total if api_total is not None else '-'):>7}  "
                  f"{state.get('status', '-'):<11} {(state.get('finished_at') or '-')[:19]}")

    orders = load(storage, "orders")
    if orders:
        created = sorted(o.get("created_at") or "" for o in orders if o.get("created_at"))
        revenue = sum(float(o.get("total") or 0) for o in orders)
        print(f"\nPedidos: {len(orders)}")
        print(f"  periodo            : {created[0][:10]} a {created[-1][:10]}")
        print(f"  faturamento bruto  : R$ {revenue:,.2f}")
        print(f"  status             : {top(Counter(o.get('status') for o in orders))}")
        print(f"  pagamento          : {top(Counter(o.get('payment_status') for o in orders))}")
        print(f"  fulfillment        : {top(Counter(o.get('fulfillment_status') for o in orders))}")
        print(f"  meio de pagamento  : {top(Counter((o.get('payment') or {}).get('method') for o in orders))}")
        print(f"  itens por pedido   : {sum(len(o.get('items') or []) for o in orders) / len(orders):.1f} em media")
        with_doc = sum(1 for o in orders if (o.get("customer") or {}).get("cgc"))
        print(f"  com CPF/CNPJ       : {with_doc} ({pct(with_doc, len(orders))})")
        print(f"  cancelados         : {sum(1 for o in orders if o.get('canceled_at'))}")

    customers = load(storage, "customers")
    if customers:
        with_doc = sum(1 for c in customers if c.get("cgc"))
        with_phone = sum(1 for c in customers if c.get("phone"))
        print(f"\nClientes: {len(customers)}")
        print(f"  com CPF/CNPJ       : {with_doc} ({pct(with_doc, len(customers))})")
        print(f"  com telefone       : {with_phone} ({pct(with_phone, len(customers))})")
        print(f"  tipo (entity)      : {top(Counter(c.get('entity') for c in customers))}")
        print(f"  newsletter         : {top(Counter(c.get('newsletter') for c in customers))}")
        print(f"  ativos             : {top(Counter(c.get('active') for c in customers))}")

    products = load(storage, "products")
    if products:
        variations = sum(len(p.get("variations") or []) for p in products)
        images = sum(len(p.get("images") or []) for p in products)
        print(f"\nProdutos: {len(products)}")
        print(f"  ativos             : {top(Counter(p.get('active') for p in products))}")
        print(f"  variacoes          : {variations} ({variations / len(products):.1f} por produto)")
        print(f"  imagens            : {images}")
        print(f"  com marca          : {sum(1 for p in products if p.get('brand_id'))}")

    for name, label in (("posts", "Posts do blog"), ("pages", "Paginas"),
                        ("hotsites", "Hotsites"), ("redirects", "Redirects"),
                        ("discounts", "Descontos")):
        count = storage.count(name)
        if count:
            print(f"\n{label}: {count}")

    summary = storage.asset_summary()
    if summary:
        print("\nAssets:")
        for status, (count, size) in sorted(summary.items()):
            print(f"  {status:<10} {count:>6} arquivo(s)  {size / 1_048_576:.1f} MB")

    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
