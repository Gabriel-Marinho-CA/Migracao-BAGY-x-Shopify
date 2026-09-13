#!/usr/bin/env python
"""PONTO DE ENTRADA DA TRANSFORMACAO - e neste arquivo que voce da play.

Le data/bagy.sqlite (gerado por run_extraction.py) e gera os payloads prontos
para a Shopify em data/shopify.sqlite e data/shopify/*.jsonl, cada um validado
contra o schema da Admin API. Nada e enviado para a Shopify.

Seguro rodar quantas vezes quiser: cada rodada refaz as entidades do zero.

    python run_transform.py                    # transforma tudo
    python run_transform.py --only pedidos     # grupo: catalogo, clientes, marketing, conteudo, pedidos
    python run_transform.py --strict           # sai com erro se houver payload invalido

ATENCAO: data/ contem dados pessoais de clientes (CPF, e-mail, endereco).
"""

import sys

from bagy_transform.transform import main

if __name__ == "__main__":
    # Saida linha a linha tambem quando rodado pela IDE.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
