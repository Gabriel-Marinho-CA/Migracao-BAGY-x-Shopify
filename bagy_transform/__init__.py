"""Fase 2 da migracao: transforma os dados extraidos da Bagy em payloads da Shopify.

Le data/bagy.sqlite (gerado por run_extraction.py) e produz, para cada registro,
as variaveis exatas da mutation da Shopify que o criara, validadas offline
contra o schema da Admin API. Nada e enviado para a Shopify.

IDs que so vao existir depois da carga (colecao, variante, cliente...) entram
como referencias `bagy-ref:<tipo>:<chave>`, resolvidas na fase de carga.
"""

__version__ = "0.1.0"
