"""Autorizacao do app pelo navegador (authorization code grant).

Loja de cliente (fora da organizacao do app no Dev Dashboard) nao aceita client
credentials. Aqui o dono da loja autoriza o app uma vez; o codigo volta para um
servidor local em http://localhost:<porta>/callback, e trocado por um token com
refresh token (expiring=1) e salvo em state/shopify_oauth.json.

Uso: run_shopify_auth.py.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from .auth import AuthError, TokenFile, entry_from_response, request_token, token_provider
from .config import ROOT, _normalize_domain, load_dotenv
from .shopify_client import ShopifyClient, ShopifyError

SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")
CALLBACK_TIMEOUT_SECONDS = 300

# O que o snapshot e a carga leem (write_ implica read_).
READ_SCOPES = ["read_products", "read_customers", "read_orders", "read_all_orders", "read_discounts",
               "read_content", "read_online_store_pages", "read_online_store_navigation",
               "read_legal_policies", "read_locations", "read_publications", "read_store_credit_accounts"]


@dataclass(frozen=True)
class OAuthSettings:
    store_domain: str
    api_version: str
    scopes: str
    port: int
    redirect_uri: str
    token_file: Path
    admin_token: str = field(default="", repr=False)
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)


def get_settings() -> OAuthSettings:
    load_dotenv()
    port = int(os.getenv("SHOPIFY_OAUTH_PORT", "3456"))
    token_file = Path(os.getenv("SHOPIFY_TOKEN_FILE", "state/shopify_oauth.json"))
    return OAuthSettings(
        store_domain=_normalize_domain(os.getenv("SHOPIFY_STORE_DOMAIN", "")),
        api_version=os.getenv("SHOPIFY_API_VERSION", "2026-07").strip(),
        scopes=",".join(s.strip() for s in os.getenv("SHOPIFY_SCOPES", "").split(",") if s.strip()),
        port=port,
        redirect_uri=os.getenv("SHOPIFY_OAUTH_REDIRECT_URI", "").strip() or f"http://localhost:{port}/callback",
        token_file=token_file if token_file.is_absolute() else ROOT / token_file,
        admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        client_id=os.getenv("SHOPIFY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", "").strip(),
    )


def verify_hmac(params: list, secret: str) -> bool:
    """HMAC-SHA256 do retorno: parametros sem `hmac`, ordenados, `k=v` unidos por `&`."""
    given = next((value for key, value in params if key == "hmac"), None)
    if not given:
        return False
    message = "&".join(f"{key}={value}" for key, value in sorted((k, v) for k, v in params if k != "hmac"))
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, given)


def authorize_url(settings: OAuthSettings, state: str) -> str:
    query = urlencode({"client_id": settings.client_id, "scope": settings.scopes,
                       "redirect_uri": settings.redirect_uri, "state": state})
    return f"https://{settings.store_domain}/admin/oauth/authorize?{query}"


def wait_for_callback(port: int, path: str, timeout: int) -> list | None:
    """Sobe um servidor local ate chegar o retorno da Shopify (ou estourar o tempo)."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            result["params"] = parse_qsl(parsed.query, keep_blank_values=True)
            body = ("<html><body style='font-family:sans-serif'><h2>Pronto!</h2>"
                    "<p>Pode fechar esta aba e voltar para o terminal.</p></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # a URL do retorno tem o codigo: nao vai para o console
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while "params" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return result.get("params")


def authorize(settings: OAuthSettings, *, open_browser: bool = True) -> int:
    missing = [name for name, value in (("SHOPIFY_STORE_DOMAIN", settings.store_domain),
                                        ("SHOPIFY_CLIENT_ID", settings.client_id),
                                        ("SHOPIFY_CLIENT_SECRET", settings.client_secret),
                                        ("SHOPIFY_SCOPES", settings.scopes)) if not value]
    if missing:
        print("ERRO: faltam no .env: " + ", ".join(missing))
        return 2
    if not SHOP_RE.match(settings.store_domain):
        print(f"ERRO: SHOPIFY_STORE_DOMAIN deve ser o dominio .myshopify.com (veio {settings.store_domain!r})")
        return 2

    state = secrets.token_urlsafe(24)
    url = authorize_url(settings, state)
    print("Antes, no Dev Dashboard do app (uma vez):")
    print(f"  - 'Allowed redirection URL(s)' da versao ativa precisa ter exatamente: {settings.redirect_uri}")
    print(f"  - o app precisa poder ser instalado em {settings.store_domain} (distribuicao para essa loja)")
    print(f"\nAbrindo o navegador para autorizar. Se nao abrir, cole este link:\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    print(f"Aguardando o retorno em {settings.redirect_uri} (ate {CALLBACK_TIMEOUT_SECONDS // 60} min)...")

    try:
        params = wait_for_callback(settings.port, urlparse(settings.redirect_uri).path or "/",
                                   CALLBACK_TIMEOUT_SECONDS)
    except OSError as exc:
        print(f"ERRO: nao deu para abrir a porta {settings.port} ({exc.__class__.__name__}). "
              "Troque SHOPIFY_OAUTH_PORT (e a URL no Dev Dashboard).")
        return 2
    if params is None:
        print("ERRO: o retorno nao chegou a tempo. Confira a URL de redirecionamento no Dev Dashboard.")
        return 1

    data = dict(params)
    if data.get("error"):
        print(f"ERRO: autorizacao recusada: {data.get('error_description') or data['error']}")
        return 1
    if not hmac.compare_digest(data.get("state", ""), state):
        print("ERRO: o state do retorno nao confere (retorno de outra tentativa?). Rode de novo.")
        return 1
    if not verify_hmac(params, settings.client_secret):
        print("ERRO: HMAC do retorno invalido. Confira SHOPIFY_CLIENT_SECRET.")
        return 1
    shop = data.get("shop", "")
    if not SHOP_RE.match(shop) or shop.lower() != settings.store_domain.lower():
        print(f"ERRO: o retorno veio de outra loja ({shop!r}).")
        return 1
    if not data.get("code"):
        print("ERRO: o retorno veio sem codigo de autorizacao.")
        return 1

    try:
        body = request_token(f"https://{settings.store_domain}/admin/oauth/access_token", {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "code": data["code"],
            "expiring": "1",
        })
    except AuthError as exc:
        print(f"ERRO ao trocar o codigo pelo token: {exc}")
        return 1

    entry = entry_from_response(body)
    TokenFile(settings.token_file).save(settings.store_domain, entry)
    granted = [scope for scope in str(entry.get("scope") or "").split(",") if scope]
    expires = (datetime.fromtimestamp(entry["expires_at"]).strftime("%d/%m %H:%M")
               if entry.get("expires_at") else "nao expira")
    print(f"\nToken salvo em {settings.token_file} (segredo; state/ fica fora do git)")
    print(f"Escopos concedidos: {len(granted)} | expira: {expires} | "
          f"renovacao automatica: {'sim' if entry.get('refresh_token') else 'nao'}")
    return 0


ORDER_QUERY = """
query ProbeOrder {
  orders(first: 1, sortKey: PROCESSED_AT, reverse: true) {
    nodes {
      id
      name
      processedAt
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      sourceName
      app {
        name
      }
      tags
      totalPriceSet {
        shopMoney {
          amount
          currencyCode
        }
      }
      lineItems(first: 5) {
        nodes {
          title
          quantity
          sku
        }
      }
    }
  }
}
"""


def check_connection(settings: OAuthSettings) -> int:
    """Le a loja, os escopos e o pedido mais recente (sem dados pessoais)."""
    try:
        provider, token, label = token_provider(
            settings.store_domain, client_id=settings.client_id, client_secret=settings.client_secret,
            admin_token=settings.admin_token, token_file=settings.token_file)
        if not (provider or token):
            print("ERRO: sem credencial para esta loja. Rode run_shopify_auth.py sem argumentos.")
            return 2
        client = ShopifyClient(settings.store_domain, token, settings.api_version, orders_per_minute=0,
                               max_retries=2, token_provider=provider)
        print(f"\nAuth: {label}")
        data = client.execute("{ shop { name myshopifyDomain createdAt currencyCode } "
                              "currentAppInstallation { app { title } accessScopes { handle } } }")
    except AuthError as exc:
        print(f"ERRO de autenticacao: {exc}")
        return 1
    except ShopifyError as exc:
        print(f"ERRO: {' '.join(str(exc).split())[:300]}")
        return 1

    shop = data["shop"]
    scopes = {scope["handle"] for scope in data["currentAppInstallation"]["accessScopes"]}
    print(f"Loja: {shop['name']} ({shop['myshopifyDomain']}), criada em {shop['createdAt'][:10]}, {shop['currencyCode']}")
    print(f"App : {data['currentAppInstallation']['app']['title']} com {len(scopes)} escopos")
    missing = [scope for scope in READ_SCOPES
               if scope not in scopes and scope.replace("read_", "write_", 1) not in scopes]
    print("Escopos de leitura que faltam: " + (", ".join(missing) if missing else "nenhum"))

    try:
        count = client.execute("{ ordersCount { count precision } }")["ordersCount"]
        print(f"Pedidos visiveis: {count['count']}{'' if count['precision'] == 'EXACT' else ' ou mais'}")
        nodes = client.execute(ORDER_QUERY)["orders"]["nodes"]
    except ShopifyError as exc:
        text = " ".join(str(exc).split())
        print(f"GET de pedido: FALHOU - {text[:300]}")
        if "protected" in text.lower() or "access" in text.lower():
            print("  Falta a aprovacao de Protected Customer Data ou o escopo read_orders.")
        return 1
    if not nodes:
        print("GET de pedido: ok, mas nao ha pedido visivel")
        return 0
    order = nodes[0]
    print("GET de pedido: ok (pedido mais recente, sem dados do cliente)")
    print(json.dumps({key: order[key] for key in ("id", "name", "processedAt", "createdAt", "displayFinancialStatus",
                                                  "displayFulfillmentStatus", "sourceName", "app", "tags",
                                                  "totalPriceSet")}, ensure_ascii=False, indent=2))
    for item in order["lineItems"]["nodes"]:
        print(f"  - {item['quantity']}x {item['title']} (sku {item['sku'] or '-'})")
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_shopify_auth",
                                     description="Autoriza o app na loja pelo navegador e testa a conexao.")
    parser.add_argument("--renew", action="store_true", help="autoriza de novo mesmo com token salvo")
    parser.add_argument("--no-browser", action="store_true", help="so imprime o link, sem abrir o navegador")
    args = parser.parse_args(argv)

    settings = get_settings()
    print("=" * 72)
    print(f"  AUTORIZACAO DO APP NA LOJA {settings.store_domain or '(defina SHOPIFY_STORE_DOMAIN)'}")
    print("=" * 72)
    saved = TokenFile(settings.token_file).get(settings.store_domain)
    if saved and not args.renew:
        print("Ja existe token salvo para esta loja: so testando (use --renew para autorizar de novo).")
    else:
        code = authorize(settings, open_browser=not args.no_browser)
        if code:
            return code
    return check_connection(settings)
