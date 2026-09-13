"""Fase 3: carga dos payloads da transformacao na loja Shopify.

Segue o diff (tabela `existing`): so envia o que esta como create/update,
resolve as `bagy-ref` com o que ja existe (`ref_map`) e com o que a propria
carga criou (`load_refs`), e guarda o progresso item a item (`load_items`).
"""
