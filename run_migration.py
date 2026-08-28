#!/usr/bin/env python
"""PONTO DE ENTRADA - e neste arquivo que voce da play.

Roda a migracao completa dos pedidos da Bagy para a Shopify.

Seguro rodar quantas vezes quiser: a migracao e idempotente. Pedidos ja criados
sao pulados (confere no ledger local E por tag na propria loja), entao apertar
play de novo nao duplica nada.

Para outros modos, use a CLI direto:

    python -m bagy2shopify.migrate --dry-run       # nao envia nada
    python -m bagy2shopify.migrate --check         # so testa as credenciais
    python -m bagy2shopify.migrate --order-id 1167 # um pedido especifico
    python -m bagy2shopify.migrate --limit 2       # no maximo 2 pedidos

Este arquivo tambem aceita as mesmas flags:

    python run_migration.py --dry-run
"""

import sys

from bagy2shopify.migrate import main

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print("=" * 72)
        print("  MIGRACAO BAGY -> SHOPIFY")
        print("  Modo: envio real (idempotente - nao duplica o que ja foi)")
        print("  Para simular sem enviar: python run_migration.py --dry-run")
        print("=" * 72)
        print()

    sys.exit(main(args))
