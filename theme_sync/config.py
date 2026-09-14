"""Loja, credencial e pastas do sync do tema."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME_DIR = ROOT / "theme"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_DIR = ROOT / "state"
BAGY_DB = ROOT / "data" / "bagy.sqlite"
BAGY_CDN = "https://cdn.dooca.store/159387/"
EXPECTED_SHOP = "mad-4-life-3m7m3sb5.myshopify.com"


def load_env(path: Path = ROOT / ".env") -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv:
        load_dotenv(path)
        return
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class ThemeSettings:
    store_domain: str
    api_version: str
    client_id: str
    client_secret: str
    admin_token: str
    token_file: Path
    theme_id: str
    expected_shop: str


def get_settings() -> ThemeSettings:
    load_env()
    token_file = Path(os.getenv("SHOPIFY_TOKEN_FILE", "state/shopify_oauth.json").strip())
    if not token_file.is_absolute():
        token_file = ROOT / token_file
    return ThemeSettings(
        store_domain=os.getenv("SHOPIFY_STORE_DOMAIN", "").strip(),
        api_version=os.getenv("SHOPIFY_API_VERSION", "2026-07").strip(),
        client_id=os.getenv("SHOPIFY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", "").strip(),
        admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        token_file=token_file,
        # Vazio = tema publicado (MAIN).
        theme_id=os.getenv("THEME_ID", "").strip(),
        expected_shop=os.getenv("THEME_EXPECTED_SHOP", EXPECTED_SHOP).strip(),
    )


def connect(settings: ThemeSettings, log=print):
    from bagy2shopify.auth import token_provider
    from bagy2shopify.shopify_client import ShopifyClient

    provider, token, label = token_provider(
        settings.store_domain, client_id=settings.client_id, client_secret=settings.client_secret,
        admin_token=settings.admin_token, token_file=settings.token_file)
    if not (provider or token):
        raise SystemExit("ERRO: sem credencial da Shopify: rode run_shopify_auth.py")
    client = ShopifyClient(settings.store_domain, token, settings.api_version, orders_per_minute=0,
                           max_retries=5, logger=log, token_provider=provider)
    return client, label
