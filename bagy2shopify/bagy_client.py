"""Leitura dos pedidos da Bagy.

Duas implementacoes com a mesma interface:

* `MockBagyClient`  - le os JSONs em ./mocks (usado hoje, sem credenciais).
* `HttpBagyClient`  - chama a API real; so precisa das credenciais para ligar.

A interface imita o comportamento real da API: primeiro pagina a listagem, e
para cada pedido da pagina busca o detalhe completo (a listagem devolve apenas
os IDs dos itens, sem preco nem nome).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Protocol


class BagyClient(Protocol):
    def list_orders(self, page: int) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...


# --------------------------------------------------------------------------- #
# Mock
# --------------------------------------------------------------------------- #

class MockBagyClient:
    """Le as respostas gravadas em disco, respeitando a paginacao."""

    def __init__(self, mock_dir: Path):
        self.mock_dir = Path(mock_dir)
        if not self.mock_dir.exists():
            raise FileNotFoundError(
                f"Diretorio de mocks nao encontrado: {self.mock_dir}. "
                "Rode `python tools/gen_mocks.py` primeiro."
            )

    def list_orders(self, page: int) -> dict:
        path = self.mock_dir / f"orders_page_{page}.json"
        if not path.exists():
            return {"paging": {"total": 0, "page": page, "limit": 0}, "Orders": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def get_order(self, order_id: str) -> dict:
        path = self.mock_dir / f"order_{order_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Detalhe do pedido {order_id} nao encontrado em {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload["Order"]


# --------------------------------------------------------------------------- #
# API real
# --------------------------------------------------------------------------- #

class HttpBagyClient:
    """Cliente da API real da Bagy/CommerceSuite.

    Preencha `base_url` e as credenciais quando forem liberadas. O formato de
    autenticacao varia por conta (access_token no header ou na query string),
    por isso os dois modos estao previstos aqui.
    """

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        auth_in_query: bool = True,
        limit: int = 30,
        timeout: int = 30,
    ):
        import requests  # import tardio: o fluxo de mock nao precisa de rede

        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.auth_in_query = auth_in_query
        self.limit = limit
        self.timeout = timeout
        self.session = requests.Session()
        if not auth_in_query:
            self.session.headers["Authorization"] = f"Bearer {access_token}"

    def _params(self, extra: dict | None = None) -> dict:
        params = dict(extra or {})
        if self.auth_in_query:
            params["access_token"] = self.access_token
        return params

    def list_orders(self, page: int) -> dict:
        response = self.session.get(
            f"{self.base_url}/orders",
            params=self._params({"page": page, "limit": self.limit, "sort": "id_asc"}),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_order(self, order_id: str) -> dict:
        response = self.session.get(
            f"{self.base_url}/orders/{order_id}",
            params=self._params(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["Order"]


# --------------------------------------------------------------------------- #
# Iteracao
# --------------------------------------------------------------------------- #

def iter_orders(client: BagyClient, *, start_page: int = 1, max_pages: int | None = None) -> Iterator[dict]:
    """Percorre todas as paginas e devolve o detalhe completo de cada pedido."""
    page = start_page
    seen_pages = 0

    while True:
        listing = client.list_orders(page)
        entries = listing.get("Orders") or []
        if not entries:
            return

        for entry in entries:
            summary = entry.get("Order", entry)
            order_id = str(summary["id"])
            yield client.get_order(order_id)

        seen_pages += 1
        if max_pages is not None and seen_pages >= max_pages:
            return

        paging = listing.get("paging") or {}
        total = int(paging.get("total") or 0)
        limit = int(paging.get("limit") or len(entries)) or len(entries)
        offset = int(paging.get("offset") or (page - 1) * limit)
        if offset + len(entries) >= total:
            return

        page += 1
