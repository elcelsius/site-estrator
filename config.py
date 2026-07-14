"""Carregamento centralizado das configurações do projeto."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _load_environment_file() -> None:
    """Carrega o arquivo ``.env`` com ou sem a biblioteca python-dotenv."""

    try:
        from dotenv import load_dotenv
    except ImportError:
        env_path = PROJECT_ROOT / ".env"
        if not env_path.exists():
            return

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(
                name.strip(),
                value.strip().strip('"').strip("'"),
            )
    else:
        load_dotenv(PROJECT_ROOT / ".env")


def _read_bool(name: str, default: bool) -> bool:
    """Interpreta uma variável de ambiente como valor booleano."""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _read_int(name: str, default: int, minimum: int = 0) -> int:
    """Lê um inteiro do ambiente e aplica um limite inferior."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return max(minimum, int(raw_value))
    except ValueError:
        return default


def _read_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Lê um número decimal do ambiente e aplica um limite inferior."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return max(minimum, float(raw_value))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class CrawlerSettings:
    """Configurações compartilhadas pelo crawler, pela CLI e pela interface."""

    base_url: str
    same_domain_only: bool
    headless: bool
    max_pages: int
    max_depth: int
    output_dir: Path
    pdf_dir: Path
    output_format: str
    navigation_timeout_ms: int
    request_timeout_ms: int
    politeness_delay: float
    respect_robots_txt: bool
    user_agent: str


def load_settings() -> CrawlerSettings:
    """Monta as configurações a partir do ambiente e de valores seguros."""

    output_format = os.getenv("OUTPUT_FORMAT", "txt").strip().lower()
    if output_format not in {"txt", "md"}:
        output_format = "txt"

    return CrawlerSettings(
        base_url=os.getenv("DEFAULT_BASE_URL", "https://example.com"),
        same_domain_only=_read_bool("SAME_DOMAIN_ONLY", True),
        headless=_read_bool("HEADLESS", True),
        max_pages=_read_int("MAX_PAGES", 500, minimum=1),
        max_depth=_read_int("MAX_DEPTH", 2),
        output_dir=Path(os.getenv("OUTPUT_DIR", "data/0_extracao_bruta")).expanduser(),
        pdf_dir=Path(os.getenv("PDF_OUTPUT_DIR", "pdf/originais")).expanduser(),
        output_format=output_format,
        navigation_timeout_ms=_read_int(
            "NAVIGATION_TIMEOUT_MS",
            120_000,
            minimum=1_000,
        ),
        request_timeout_ms=_read_int(
            "REQUEST_TIMEOUT_MS",
            60_000,
            minimum=1_000,
        ),
        politeness_delay=_read_float("POLITENESS_DELAY", 0.6),
        respect_robots_txt=_read_bool("RESPECT_ROBOTS_TXT", True),
        user_agent=os.getenv("CRAWLER_USER_AGENT", "SiteExtractor/1.0").strip()
        or "SiteExtractor/1.0",
    )


def ensure_directories(*directories: str | Path) -> None:
    """Cria os diretórios de saída que ainda não existem."""

    for directory in directories:
        Path(directory).expanduser().mkdir(parents=True, exist_ok=True)


_load_environment_file()
