#!/usr/bin/env python
"""PONTO DE ENTRADA DA AUTORIZACAO NA SHOPIFY - e neste arquivo que voce da play.

Loja de cliente (fora da organizacao do app no Dev Dashboard) nao aceita client
credentials ("Client credentials cannot be performed on this shop"). Aqui o app
e autorizado uma vez pelo navegador; o token fica em state/shopify_oauth.json e
e renovado sozinho pelos outros scripts.

    python run_shopify_auth.py            # 1a vez: abre o navegador; depois: so testa
    python run_shopify_auth.py --renew    # autoriza de novo (token revogado, escopos novos)

Ao final le a loja, os escopos e o pedido mais recente (sem dados do cliente).

Precisa no .env: SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET e
SHOPIFY_SCOPES. No Dev Dashboard, a URL http://localhost:3456/callback precisa
estar em "Allowed redirection URL(s)".
"""

import sys

from bagy2shopify.oauth import main

if __name__ == "__main__":
    # Saida linha a linha tambem quando rodado pela IDE.
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main(sys.argv[1:]))
