#!/usr/bin/env python
"""PONTO DE ENTRADA DO SNAPSHOT + DIFF - e neste arquivo que voce da play.

A loja de destino ja teve uma tentativa de migracao. Este script:

1. le o que ja existe na Shopify (produtos, clientes, pedidos, descontos,
   paginas, redirecionamentos...) e guarda em data/shopify_existing.sqlite;
2. compara com os payloads de run_transform.py e marca cada um como criar,
   atualizar, ja existe, conflito ou nao conferido (data/shopify.sqlite,
   tabelas `existing` e `ref_map`).

SO LEITURA: nada e criado, alterado ou apagado na loja.

    python run_shopify_snapshot.py                  # le a loja e faz o diff
    python run_shopify_snapshot.py --only pedidos   # grupos: catalogo, clientes, marketing, conteudo, pedidos
    python run_shopify_snapshot.py --diff-only      # refaz so o diff (sem rede), ex.: depois de run_transform.py
    python run_shopify_snapshot.py --list           # o que ja foi lido e quando

Precisa no .env: SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET.
ATENCAO: data/ contem dados pessoais de clientes.
"""

import sys

from shopify_snapshot.cli import main

if __name__ == "__main__":
    # Saida linha a linha tambem quando rodado pela IDE.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
