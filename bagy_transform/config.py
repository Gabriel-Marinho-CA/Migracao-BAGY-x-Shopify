"""Configuracao da transformacao, lida do .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bagy2shopify.config import ROOT, load_dotenv
from bagy_extract.config import get_settings as get_extract_settings


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "nao", "")


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else ROOT / value


def _schema_path(version: str) -> Path:
    candidates = [
        ROOT / ".claude" / "skills" / "shopify-admin" / "assets" / f"admin_{version}.json.gz",
        Path.home() / ".claude" / "skills" / "shopify-admin" / "assets" / f"admin_{version}.json.gz",
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


@dataclass(frozen=True)
class TransformSettings:
    source_db: Path
    out_db: Path
    out_dir: Path
    schema_path: Path
    api_version: str
    currency: str
    timezone_offset: str
    fulfill_invoiced_after_days: int
    order_name_from_code: bool
    include_leads: bool
    include_inactive_discounts: bool
    blog_title: str
    blog_handle: str
    article_author: str


def get_settings() -> TransformSettings:
    load_dotenv()
    extract = get_extract_settings()
    version = os.getenv("SHOPIFY_API_VERSION", "2026-07").strip()
    return TransformSettings(
        source_db=extract.db_path,
        out_db=_path("TRANSFORM_DB", "data/shopify.sqlite"),
        out_dir=_path("TRANSFORM_OUT_DIR", "data/shopify"),
        schema_path=_schema_path(version),
        api_version=version,
        currency=os.getenv("SHOPIFY_CURRENCY", "BRL").strip().upper(),
        # Horarios da Bagy sao locais (-03:00): confirmado comparando com o
        # Pagar.me, que grava as mesmas transacoes em UTC.
        timezone_offset=os.getenv("BAGY_TIMEZONE_OFFSET", "-03:00").strip(),
        # Pedidos com NF-e emitida mas sem envio registrado: acima deste numero
        # de dias sao considerados enviados (ver README).
        fulfill_invoiced_after_days=int(os.getenv("TRANSFORM_FULFILL_INVOICED_AFTER_DAYS", "30")),
        order_name_from_code=_flag("TRANSFORM_ORDER_NAME_FROM_CODE", "1"),
        include_leads=_flag("TRANSFORM_INCLUDE_LEADS", "1"),
        include_inactive_discounts=_flag("TRANSFORM_INCLUDE_INACTIVE_DISCOUNTS", "0"),
        blog_title=os.getenv("TRANSFORM_BLOG_TITLE", "Blog").strip(),
        blog_handle=os.getenv("TRANSFORM_BLOG_HANDLE", "blog").strip(),
        article_author=os.getenv("TRANSFORM_ARTICLE_AUTHOR", "Mad 4 Life").strip(),
    )
