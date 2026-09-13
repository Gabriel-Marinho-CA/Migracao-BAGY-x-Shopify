#!/usr/bin/env python
"""PONTO DE ENTRADA DA EXTRACAO - e neste arquivo que voce da play.

Le todos os dados da loja na Bagy e grava em data/bagy.sqlite. So leitura:
nada e alterado na Bagy e nada e enviado para a Shopify.

Seguro rodar quantas vezes quiser: registros sao atualizados pela chave, e uma
rodada interrompida retoma da pagina onde parou.

    python run_extraction.py                     # extrai tudo
    python run_extraction.py --plan              # totais e requisicoes previstas
    python run_extraction.py --list              # catalogo e o que ja esta no banco
    python run_extraction.py --only pedidos      # um grupo (ou recurso)
    python run_extraction.py --export --assets   # + JSON por recurso + backup das imagens

ATENCAO: data/ contem dados pessoais de clientes (CPF, e-mail, endereco).
"""

import sys

from bagy_extract.extract import main

if __name__ == "__main__":
    # Sem isso, ao rodar pela IDE (saida em pipe, nao terminal) o Python
    # bufferiza tudo e nada aparece ate o fim - minutos de tela vazia.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
