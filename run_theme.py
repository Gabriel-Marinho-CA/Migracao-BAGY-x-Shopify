#!/usr/bin/env python
"""PONTO DE ENTRADA DO TEMA - e neste arquivo que voce da play.

Clona o frontend da mad4.life no tema publicado da loja MAD 4 Life. ESCREVE NA LOJA.

    python run_theme.py                         # tudo: imagens, templates, menus, envio e atribuicao
    python run_theme.py --dry-run               # mostra o que faria
    python run_theme.py --only push             # so envia os arquivos de theme/ que mudaram
    python run_theme.py --only push --file sections/mad-footer.liquid
    python run_theme.py --only templates --only push --force   # regrava templates editados no admin

Codigo do tema em theme/ (secoes mad-*, assets/mad-theme.css e .js). Templates
JSON sao gerados por theme_sync/layouts.py: os de produto e pagina saem dos
hotsites da Bagy (data/bagy.sqlite). Rodar de novo so envia o que mudou.
"""

import sys

from theme_sync.cli import main

if __name__ == "__main__":
    # Saida linha a linha tambem quando rodado pela IDE.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
