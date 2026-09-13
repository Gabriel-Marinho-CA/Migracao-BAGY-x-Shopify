"""Configuracao do extrator, lida do .env (mesmo loader do bagy2shopify)."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

from bagy2shopify.config import ROOT, load_dotenv


@dataclass(frozen=True)
class ExtractSettings:
    api_base: str
    api_token: str
    requests_per_minute: float
    max_retries: int
    page_size: int
    timeout: int
    db_path: Path
    export_dir: Path
    assets_dir: Path
    log_dir: Path
    asset_hosts: tuple
    asset_requests_per_minute: float

    @property
    def has_token(self) -> bool:
        return bool(self.api_token)


def _path(env: str, default: str) -> Path:
    """Caminhos relativos sao resolvidos a partir da raiz do projeto."""
    value = Path(os.getenv(env, default))
    return value if value.is_absolute() else ROOT / value


def token_info(token: str) -> dict:
    """Le (sem validar) o payload do JWT da Bagy: shop_id, nome da integracao.

    Serve so para mostrar de qual loja o token e, e evitar extrair a loja errada.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
    except Exception:
        return {}


def get_settings() -> ExtractSettings:
    load_dotenv()
    hosts = os.getenv("EXTRACT_ASSET_HOSTS", "cdn.dooca.store,api4.dooca.store")
    return ExtractSettings(
        api_base=os.getenv("BAGY_API_BASE", "https://api.dooca.store").strip().rstrip("/"),
        api_token=os.getenv("BAGY_API_TOKEN", "").strip(),
        # O limite real nao e publico (vem num checklist de parceiro). 60/min e
        # conservador; ha backoff automatico se a API devolver 429.
        requests_per_minute=float(os.getenv("BAGY_REQUESTS_PER_MINUTE", "60")),
        max_retries=int(os.getenv("BAGY_MAX_RETRIES", "5")),
        # 150 e o maximo aceito pela maioria dos recursos.
        page_size=int(os.getenv("BAGY_PAGE_SIZE", "150")),
        timeout=int(os.getenv("BAGY_TIMEOUT", "120")),
        db_path=_path("EXTRACT_DB", "data/bagy.sqlite"),
        export_dir=_path("EXTRACT_EXPORT_DIR", "data/export"),
        assets_dir=_path("EXTRACT_ASSETS_DIR", "data/assets"),
        log_dir=_path("MIGRATION_LOG_DIR", "logs"),
        asset_hosts=tuple(h.strip().lower() for h in hosts.split(",") if h.strip()),
        asset_requests_per_minute=float(os.getenv("EXTRACT_ASSET_REQUESTS_PER_MINUTE", "240")),
    )
