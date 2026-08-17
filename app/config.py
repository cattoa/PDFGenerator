"""Centralized runtime configuration, sourced from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings. Override via `PDFGEN_*` environment variables or a `.env` file."""

    model_config = SettingsConfigDict(env_prefix="PDFGEN_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    templates_dir: Path = _PROJECT_ROOT / "templates"
    output_dir: Path = _PROJECT_ROOT / "output"
    host: str = "127.0.0.1"
    port: int = 8000

    @field_validator("templates_dir", "output_dir")
    @classmethod
    def _resolve_absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached — construct once per process)."""
    return Settings()
