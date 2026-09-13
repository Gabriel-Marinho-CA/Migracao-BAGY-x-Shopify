"""Configuracao da carga, lida do .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from bagy2shopify.config import _normalize_domain, load_dotenv
from bagy_transform.config import _flag, _path
from bagy_transform.config import get_settings as get_transform_settings


@dataclass(frozen=True)
class LoadSettings:
    store_domain: str
    api_version: str
    transform_db: Path
    snapshot_db: Path
    out_dir: Path
    token_file: Path
    log_dir: Path
    log_payloads: bool
    orders_per_minute: float
    max_retries: int
    product_images: bool
    pilot_orders: int
    admin_token: str = field(default="", repr=False)
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)


def get_settings() -> LoadSettings:
    load_dotenv()
    transform = get_transform_settings()
    return LoadSettings(
        store_domain=_normalize_domain(os.getenv("SHOPIFY_STORE_DOMAIN", "")),
        api_version=transform.api_version,
        transform_db=transform.out_db,
        snapshot_db=_path("SNAPSHOT_DB", "data/shopify_existing.sqlite"),
        out_dir=transform.out_dir,
        token_file=_path("SHOPIFY_TOKEN_FILE", "state/shopify_oauth.json"),
        log_dir=_path("MIGRATION_LOG_DIR", "logs"),
        log_payloads=_flag("MIGRATION_LOG_PAYLOADS", "1"),
        orders_per_minute=float(os.getenv("SHOPIFY_ORDERS_PER_MINUTE", "5")),
        max_retries=int(os.getenv("SHOPIFY_MAX_RETRIES", "5")),
        # Imagens de produto no productSet: a Shopify baixa cada uma da URL do
        # CDN da Bagy. Produto ja carregado sem imagem: run_load.py --images.
        product_images=_flag("LOAD_PRODUCT_IMAGES", "1"),
        pilot_orders=int(os.getenv("LOAD_PILOT_ORDERS", "10")),
        admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        client_id=os.getenv("SHOPIFY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", "").strip(),
    )
