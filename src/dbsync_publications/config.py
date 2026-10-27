"""Connection settings loaded from .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

SOURCE_VAR = "SOURCE_DATABASE_URL"
TARGET_VARS = ("TARGET_1_DATABASE_URL", "TARGET_2_DATABASE_URL")
MIGRATIONS_VAR = "MIGRATIONS_DIR"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    source_url: str
    target_urls: dict[str, str]  # env var name -> url, only defined targets
    migrations_dir: Path | None = None  # bmi-data-warehouse-sql migrations/, optional


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file or ".env")
    source = os.environ.get(SOURCE_VAR, "").strip()
    if not source:
        raise ConfigError(f"{SOURCE_VAR} is not defined in .env")
    targets = {var: url for var in TARGET_VARS if (url := os.environ.get(var, "").strip())}
    if not targets:
        raise ConfigError(f"Define at least one target: {', '.join(TARGET_VARS)}")
    if source in targets.values():
        raise ConfigError("A target URL is identical to the source URL")
    migrations = os.environ.get(MIGRATIONS_VAR, "").strip()
    return Settings(source, targets, Path(migrations).expanduser() if migrations else None)


def describe(url: str) -> str:
    """Connection description without credentials."""
    info = conninfo_to_dict(url)
    return f"{info.get('host', 'localhost')}:{info.get('port', '5432')}/{info.get('dbname', '?')}"
