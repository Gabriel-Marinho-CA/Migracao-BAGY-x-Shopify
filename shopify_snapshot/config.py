"""Configuracao do snapshot e do diff, lida do .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from bagy2shopify.config import _normalize_domain, load_dotenv
from bagy_transform.config import _flag, _path
from bagy_transform.config import get_settings as get_transform_settings


@dataclass(frozen=True)
class SnapshotSettings:
    store_domain: str
    api_version: str
    db_path: Path
    transform_db: Path
    out_dir: Path
    currency: str
    poll_seconds: float
    max_retries: int
    match_by_title: bool
    # Token autorizado pelo navegador (run_shopify_auth.py). Segredo.
    token_file: Path
    # repr=False: um print(settings) nunca mostra credencial.
    admin_token: str = field(default="", repr=False)
    client_id: str = field(default="", repr=False)
    client_secret: str = field(default="", repr=False)


def get_settings() -> SnapshotSettings:
    load_dotenv()
    transform = get_transform_settings()
    return SnapshotSettings(
        store_domain=_normalize_domain(os.getenv("SHOPIFY_STORE_DOMAIN", "")),
        api_version=transform.api_version,
        db_path=_path("SNAPSHOT_DB", "data/shopify_existing.sqlite"),
        transform_db=transform.out_db,
        out_dir=transform.out_dir,
        currency=transform.currency,
        poll_seconds=float(os.getenv("SNAPSHOT_POLL_SECONDS", "5")),
        max_retries=int(os.getenv("SHOPIFY_MAX_RETRIES", "5")),
        # Casar por titulo (produto, colecao, pagina, blog, artigo, menu) quando
        # a migracao anterior usou outro handle. Fica marcado para conferir.
        match_by_title=_flag("SNAPSHOT_MATCH_BY_TITLE", "1"),
        token_file=_path("SHOPIFY_TOKEN_FILE", "state/shopify_oauth.json"),
        admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        client_id=os.getenv("SHOPIFY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("SHOPIFY_CLIENT_SECRET", "").strip(),
    )
