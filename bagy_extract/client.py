"""Cliente HTTP da API da Bagy.

A API desta loja e a `api.dooca.store` (a Bagy e a antiga Dooca), autenticada
com o token JWT em `Authorization: Bearer`.

Atencao: NAO e a API documentada em developers.bagy.com.br. Aquela documenta a
plataforma antiga (formato Tray/CommerceSuite, `web_api` + OAuth com
consumer_key) - o formato dos mocks do bagy2shopify. A documentacao certa desta
API esta na base de conhecimento: basedeconhecimento.bagy.com.br, categoria
"Documentacao da API".

Paginacao no padrao Laravel: `?limit=&page=` com `meta.last_page`. O `limit`
maximo varia por recurso (150 na maioria, 100 em mailings/all, e o
checkout/abandoned ignora o parametro e devolve 25).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import requests


class BagyApiError(RuntimeError):
    """Erro de transporte ou HTTP. Carrega o request id da borda (Azion)."""

    def __init__(self, message: str, *, status: int | None = None,
                 request_id: str | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.url = url


class BagyForbidden(BagyApiError):
    """403 - o token nao tem permissao para o recurso."""


class BagyNotFound(BagyApiError):
    """404 - o recurso nao existe nesta conta."""


@dataclass
class Page:
    number: int
    last_page: int
    total: int | None
    per_page: int | None
    items: list
    elapsed: float
    size_bytes: int


class Pacer:
    """Espaca as requisicoes de forma uniforme, sem rajadas.

    Uma janela deslizante (como a do cliente da Shopify) permitiria disparar
    as 60 requisicoes do minuto de uma vez; como o limite de curto prazo da
    Bagy nao e publico, o mais seguro e nunca concentrar chamadas.
    """

    def __init__(self, per_minute: float):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._next = 0.0

    def wait(self) -> float:
        now = time.monotonic()
        delay = self._next - now
        if delay > 0:
            time.sleep(delay)
        else:
            delay = 0.0
        self._next = max(now, self._next) + self.interval
        return delay


class BagyClient:
    def __init__(self, base_url: str, token: str, *, requests_per_minute: float = 60,
                 max_retries: int = 5, timeout: int = 120, logger=None):
        if not token:
            raise BagyApiError("BAGY_API_TOKEN ausente. Preencha no .env.")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.pacer = Pacer(requests_per_minute)
        self.log = logger or (lambda message: None)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "bagy-extract/0.1",
        })
        self.request_count = 0
        self.last_request_id: str | None = None

    def get(self, path: str, params: dict | None = None):
        """GET com retry. Devolve (corpo_json, segundos, bytes)."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0

        while True:
            attempt += 1
            waited = self.pacer.wait()
            if waited >= 2:
                self.log(f"      ritmo: aguardando {waited:.1f}s")

            started = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise BagyApiError(
                        f"falha de rede apos {self.max_retries} tentativas: {exc}", url=url
                    ) from exc
                self._backoff(attempt, f"erro de rede ({exc.__class__.__name__})")
                continue

            self.request_count += 1
            request_id = (response.headers.get("X-Azion-Request-Id")
                          or response.headers.get("X-Request-Id"))
            self.last_request_id = request_id
            status = response.status_code

            if status == 429 or status >= 500:
                if attempt > self.max_retries:
                    raise BagyApiError(
                        f"HTTP {status} apos {self.max_retries} tentativas: {response.text[:300]}",
                        status=status, request_id=request_id, url=response.url,
                    )
                self._backoff(attempt, f"HTTP {status}", response.headers.get("Retry-After"))
                continue

            if status == 401:
                raise BagyApiError("HTTP 401: token da Bagy invalido ou expirado.",
                                   status=status, request_id=request_id, url=response.url)
            if status == 403:
                raise BagyForbidden(f"HTTP 403: token sem permissao - {response.text[:200]}",
                                    status=status, request_id=request_id, url=response.url)
            if status == 404:
                raise BagyNotFound(f"HTTP 404: {path} nao existe",
                                   status=status, request_id=request_id, url=response.url)
            if status >= 400:
                raise BagyApiError(f"HTTP {status}: {response.text[:300]}",
                                   status=status, request_id=request_id, url=response.url)

            try:
                body = response.json()
            except ValueError as exc:
                raise BagyApiError(f"resposta nao-JSON: {response.text[:200]}",
                                   status=status, request_id=request_id,
                                   url=response.url) from exc

            return body, time.monotonic() - started, len(response.content)

    def _backoff(self, attempt: int, reason: str, retry_after: str | None = None) -> None:
        try:
            delay = float(retry_after) if retry_after else 2.0 ** attempt
        except ValueError:
            delay = 2.0 ** attempt
        delay = min(delay, 120.0) + random.uniform(0, 0.5)
        self.log(f"      {reason}: nova tentativa em {delay:.1f}s (tentativa {attempt})")
        time.sleep(delay)

    def page(self, path: str, number: int, *, limit: int | None, sort: str | None) -> Page:
        params: dict = {"page": number}
        if limit:
            params["limit"] = limit
        if sort:
            params["sort"] = sort
        body, elapsed, size = self.get(path, params)

        if (isinstance(body, dict) and isinstance(body.get("data"), list)
                and isinstance(body.get("meta"), dict)):
            meta = body["meta"]
            return Page(
                number=int(meta.get("current_page") or number),
                last_page=int(meta.get("last_page") or number),
                total=meta.get("total"),
                per_page=meta.get("per_page"),
                items=body["data"],
                elapsed=elapsed,
                size_bytes=size,
            )

        # Lista sem paginacao.
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            items = body["data"]
        elif isinstance(body, list):
            items = body
        else:
            items = [body]
        return Page(number=1, last_page=1, total=len(items), per_page=None,
                    items=items, elapsed=elapsed, size_bytes=size)
