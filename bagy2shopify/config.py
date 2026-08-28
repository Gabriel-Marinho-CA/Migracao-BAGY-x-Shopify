"""Configuracao lida de variaveis de ambiente (com suporte a .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Carrega um .env simples sem depender de pacote externo.

    Nao sobrescreve variaveis ja presentes no ambiente.
    """
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    # utf-8-sig: editores no Windows costumam gravar UTF-8 com BOM, e o BOM
    # entraria no nome da primeira variavel.
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    store_domain: str
    admin_token: str
    api_version: str
    currency: str
    timezone_offset: str
    mock_dir: Path
    state_file: Path
    payload_dir: Path
    log_dir: Path
    log_payloads: bool
    orders_per_minute: float
    max_retries: int

    @property
    def graphql_url(self) -> str:
        return f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json"

    @property
    def has_credentials(self) -> bool:
        return bool(self.store_domain and self.admin_token)


def _normalize_domain(value: str) -> str:
    """Aceita 'https://x.myshopify.com/' e devolve 'x.myshopify.com'."""
    value = value.strip()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.rstrip("/")


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        store_domain=_normalize_domain(os.getenv("SHOPIFY_STORE_DOMAIN", "")),
        admin_token=os.getenv("SHOPIFY_ADMIN_TOKEN", "").strip(),
        # Versoes suportadas da Admin API em 2026-08: 2025-10, 2026-01, 2026-04,
        # 2026-07 (ultima estavel), 2026-10 (RC). 2025-07 saiu de suporte.
        api_version=os.getenv("SHOPIFY_API_VERSION", "2026-07").strip(),
        currency=os.getenv("SHOPIFY_CURRENCY", "BRL").strip().upper(),
        # O Brasil nao usa mais horario de verao desde 2019, entao o offset e fixo.
        timezone_offset=os.getenv("BAGY_TIMEZONE_OFFSET", "-03:00").strip(),
        mock_dir=Path(os.getenv("BAGY_MOCK_DIR", str(ROOT / "mocks"))),
        state_file=Path(os.getenv("MIGRATION_STATE_FILE", str(ROOT / "state" / "migrated.json"))),
        payload_dir=Path(os.getenv("MIGRATION_PAYLOAD_DIR", str(ROOT / "out" / "payloads"))),
        log_dir=Path(os.getenv("MIGRATION_LOG_DIR", str(ROOT / "logs"))),
        # O payload que falhou vai junto no log, para dar reprocessar depois.
        log_payloads=os.getenv("MIGRATION_LOG_PAYLOADS", "1").strip() not in ("0", "false", "no"),
        # Lojas de desenvolvimento/trial aceitam no maximo 5 orderCreate por minuto.
        orders_per_minute=float(os.getenv("SHOPIFY_ORDERS_PER_MINUTE", "5")),
        max_retries=int(os.getenv("SHOPIFY_MAX_RETRIES", "5")),
    )
