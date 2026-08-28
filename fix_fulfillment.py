#!/usr/bin/env python
"""PONTO DE ENTRADA - rode depois da aprovacao de Protected Customer Data.

Despacha, nos pedidos ja migrados, os que estavam FINALIZADO/ENVIADO/ENTREGUE
na Bagy (hoje: 15, 1168 e 1190). Corrige os pedidos existentes no lugar, sem
criar duplicata.

Enquanto a aprovacao nao sair, este script avisa e sai sem fazer nada.

    python fix_fulfillment.py --dry-run
    python fix_fulfillment.py
"""

import sys

from bagy2shopify.fix_fulfillment import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
