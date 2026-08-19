"""Centralized runtime configuration, sourced from environment variables (or a .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The one deployment environment that gets its own isolated template store.
# Every other environment value (dev, test, uat, ...) shares the base store.
PRODUCTION_ENVIRONMENT = "Production"

# Label reported for the shared store when describing a non-production target
# (e.g. in a migrate response), since that store is not tied to one env name.
NON_PRODUCTION_ENVIRONMENT = "non-production"

# Sub-directory of the base templates dir holding the production templates.
PRODUCTION_TEMPLATES_SUBDIR = "production"


def is_production(environment: str) -> bool:
    """Whether an environment tag names the production environment.

    Matched case-insensitively, so `Production`, `production` and `PRODUCTION`
    are all treated as production; anything else is a non-production env.
    """
    return environment.strip().casefold() == PRODUCTION_ENVIRONMENT.casefold()


class Settings(BaseSettings):
    """Application settings. Override via `PDFGEN_*` environment variables or a `.env` file."""

    model_config = SettingsConfigDict(env_prefix="PDFGEN_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    templates_dir: Path = _PROJECT_ROOT / "templates"
    # Defaults to `<templates_dir>/production` — see _default_production_templates_dir.
    production_templates_dir: Path | None = None
    output_dir: Path = _PROJECT_ROOT / "output"
    host: str = "127.0.0.1"
    port: int = 8000
    # Defensive upper bound on uploaded template size. Templates are text, but
    # self-contained ones embed their web fonts and images as base64 data URIs,
    # which easily runs past a megabyte — hence the generous default.
    max_template_bytes: int = 4_194_304

    @field_validator("max_template_bytes")
    @classmethod
    def _positive_max_template_bytes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_template_bytes must be a positive number of bytes")
        return value

    @field_validator("templates_dir", "production_templates_dir", "output_dir")
    @classmethod
    def _resolve_absolute(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser().resolve()

    @model_validator(mode="after")
    def _default_production_templates_dir(self) -> Settings:
        if self.production_templates_dir is None:
            self.production_templates_dir = self.templates_dir / PRODUCTION_TEMPLATES_SUBDIR
        return self

    def templates_dir_for(self, environment: str) -> Path:
        """Return the template store backing a given environment tag."""
        if is_production(environment):
            # Never None once _default_production_templates_dir has run.
            assert self.production_templates_dir is not None
            return self.production_templates_dir
        return self.templates_dir


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached — construct once per process)."""
    return Settings()
