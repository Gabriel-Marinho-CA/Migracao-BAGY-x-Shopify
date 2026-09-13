"""Token da Admin API sem colar token no .env.

Tres jeitos, nesta ordem (ver token_provider):

1. Token autorizado pelo navegador (authorization code grant), salvo em
   state/shopify_oauth.json por run_shopify_auth.py. E o que funciona em loja de
   cliente, fora da organizacao do app. Expira e e renovado sozinho com o refresh
   token (que vale 90 dias).
2. Client credentials (SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET): so para loja
   da MESMA organizacao do app no Dev Dashboard. Vale 24 h e e pedido de novo.
   Em loja de cliente a Shopify responde "shop_not_permitted".
3. SHOPIFY_ADMIN_TOKEN fixo.

Tokens nunca sao impressos. O arquivo em state/ (fora do git) e segredo.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Renova alguns minutos antes de expirar, para nao morrer no meio de uma chamada.
MARGIN_SECONDS = 300


class AuthError(RuntimeError):
    """Falha ao obter o token. A mensagem nunca inclui token, client id nem secret."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def request_token(url: str, data: dict, *, form: bool = True, timeout: int = 30) -> dict:
    """POST em /admin/oauth/access_token. Devolve o JSON com access_token."""
    try:
        response = requests.post(url, data=data if form else None, json=None if form else data,
                                 headers={"Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as exc:
        raise AuthError(f"falha de rede ao pedir o token ({exc.__class__.__name__})") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code != 200:
        code = body.get("error") if isinstance(body.get("error"), str) else None
        detail = str(body.get("error_description") or body.get("error") or body.get("errors") or "").strip()
        raise AuthError(f"HTTP {response.status_code} ao pedir o token" + (f": {detail[:200]}" if detail else ""),
                        code)
    if not body.get("access_token"):
        raise AuthError("resposta do token veio sem access_token")
    return body


def entry_from_response(body: dict, previous: dict | None = None) -> dict:
    """Resposta do /access_token -> o que fica salvo (com os vencimentos em epoch)."""
    now = time.time()
    previous = previous or {}
    entry = {
        "access_token": body["access_token"],
        "scope": body.get("scope") or previous.get("scope"),
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if body.get("expires_in"):
        entry["expires_at"] = now + int(body["expires_in"])
    refresh_token = body.get("refresh_token") or previous.get("refresh_token")
    if refresh_token:
        entry["refresh_token"] = refresh_token
        if body.get("refresh_token_expires_in"):
            entry["refresh_expires_at"] = now + int(body["refresh_token_expires_in"])
        elif previous.get("refresh_expires_at"):
            entry["refresh_expires_at"] = previous["refresh_expires_at"]
    return entry


class TokenFile:
    """state/shopify_oauth.json: um token por loja."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, store_domain: str) -> dict | None:
        return self.load().get(store_domain)

    def save(self, store_domain: str, entry: dict) -> None:
        data = self.load()
        data[store_domain] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class OAuthToken:
    """Token autorizado pelo navegador, renovado pelo refresh token quando expira."""

    def __init__(self, store_domain: str, client_id: str, client_secret: str, token_file: TokenFile,
                 *, timeout: int = 30):
        self.store_domain = store_domain
        self.url = f"https://{store_domain}/admin/oauth/access_token"
        self._client_id = client_id
        self._client_secret = client_secret
        self.token_file = token_file
        self.timeout = timeout
        self.entry = token_file.get(store_domain)
        if not self.entry:
            raise AuthError("sem token salvo para esta loja: rode run_shopify_auth.py")

    def __repr__(self) -> str:
        return f"OAuthToken({self.store_domain!r})"

    @property
    def scopes(self) -> list:
        return sorted(scope for scope in str(self.entry.get("scope") or "").split(",") if scope)

    def __call__(self, force: bool = False) -> str:
        expires_at = self.entry.get("expires_at")
        if force or (expires_at and time.time() >= expires_at - MARGIN_SECONDS):
            self._refresh()
        return self.entry["access_token"]

    def _refresh(self) -> None:
        refresh_token = self.entry.get("refresh_token")
        if not refresh_token:
            raise AuthError("o token salvo expirou ou foi recusado e nao tem refresh token: "
                            "rode run_shopify_auth.py --renew")
        refresh_expires = self.entry.get("refresh_expires_at")
        if refresh_expires and time.time() >= refresh_expires:
            raise AuthError("o refresh token venceu (90 dias): rode run_shopify_auth.py --renew")
        if not (self._client_id and self._client_secret):
            raise AuthError("para renovar o token faltam SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET no .env")
        try:
            body = request_token(self.url, {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }, timeout=self.timeout)
        except AuthError as exc:
            raise AuthError(f"nao deu para renovar o token ({exc}): rode run_shopify_auth.py --renew",
                            exc.code) from None
        self.entry = entry_from_response(body, self.entry)
        self.token_file.save(self.store_domain, self.entry)


class ClientCredentialsToken:
    """Token de client credentials: so para loja da mesma organizacao do app."""

    def __init__(self, store_domain: str, client_id: str, client_secret: str, *, timeout: int = 30):
        if not (store_domain and client_id and client_secret):
            raise AuthError("Faltam SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID ou SHOPIFY_CLIENT_SECRET.")
        self.url = f"https://{store_domain}/admin/oauth/access_token"
        self._client_id = client_id
        self._client_secret = client_secret
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self.scopes: list = []

    def __repr__(self) -> str:
        return f"ClientCredentialsToken({self.url!r})"

    def __call__(self, force: bool = False) -> str:
        if force or not self._token or time.monotonic() >= self._expires_at:
            self._fetch()
        return self._token

    def _fetch(self) -> None:
        try:
            body = request_token(self.url, {"grant_type": "client_credentials", "client_id": self._client_id,
                                            "client_secret": self._client_secret},
                                 form=False, timeout=self.timeout)
        except AuthError as exc:
            if exc.code == "shop_not_permitted" or "cannot be performed on this shop" in str(exc).lower():
                raise AuthError("esta loja nao e da mesma organizacao do app, entao client credentials nao "
                                "funciona. Autorize o app pelo navegador uma vez: rode run_shopify_auth.py.",
                                exc.code) from None
            raise AuthError(f"{exc}. Confira SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET.", exc.code) from None
        self._token = body["access_token"]
        expires_in = int(body.get("expires_in") or 86399)
        self._expires_at = time.monotonic() + max(expires_in - MARGIN_SECONDS, 60)
        self.scopes = sorted(scope for scope in str(body.get("scope") or "").split(",") if scope)


def token_provider(store_domain: str, *, client_id: str = "", client_secret: str = "", admin_token: str = "",
                   token_file=None) -> tuple:
    """Escolhe a credencial. Devolve (provider ou None, token fixo, descricao)."""
    tokens = TokenFile(token_file) if token_file else None
    if tokens and tokens.get(store_domain):
        return (OAuthToken(store_domain, client_id, client_secret, tokens), "",
                f"token autorizado pelo navegador ({tokens.path.name}, renovado sozinho)")
    if client_id and client_secret:
        return (ClientCredentialsToken(store_domain, client_id, client_secret), "",
                "client credentials (so loja da mesma organizacao do app)")
    if admin_token:
        return None, admin_token, "SHOPIFY_ADMIN_TOKEN fixo"
    return None, "", None
