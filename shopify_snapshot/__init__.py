"""Snapshot da loja Shopify de destino e diff com os payloads da transformacao.

A loja ja passou por uma tentativa de migracao: antes de carregar, le o que
existe la (so leitura) e marca cada payload como criar, atualizar, ja existe,
conflito ou nao conferido.
"""
