#!/usr/bin/env python
"""PONTO DE ENTRADA DA CARGA NA SHOPIFY - e neste arquivo que voce da play.

Envia os payloads de run_transform.py para a loja, seguindo o diff de
run_shopify_snapshot.py (so o que esta como criar/atualizar). ESCREVE NA LOJA.

    python run_load.py                        # sem argumentos: so mostra o estado, nao envia nada
    python run_load.py --pilot --dry-run      # o que o piloto enviaria
    python run_load.py --pilot                # piloto: poucos pedidos com casos diferentes + dependencias
    python run_load.py --only catalogo        # grupos: catalogo, clientes, marketing, conteudo, pedidos
    python run_load.py --only pedidos --limit 50
    python run_load.py --all                  # tudo o que falta
    python run_load.py --images               # imagens dos produtos que ja estao na loja sem imagem

Rodar de novo continua de onde parou (data/shopify.sqlite, tabela load_items).
Nada dispara e-mail para cliente. Imagens de produto: vao no productSet e a
Shopify baixa do CDN da Bagy (LOAD_PRODUCT_IMAGES=1). --images nunca duplica:
so envia para produto com zero imagens na loja.
"""

import sys

from shopify_load.cli import main

if __name__ == "__main__":
    # Saida linha a linha tambem quando rodado pela IDE.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
