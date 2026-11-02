"""Settings from the environment (CONTRACTS §9)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_url: str | None = None
    ingest_token: str | None = None
    raw_store: str = "file://./.data/raw"
    user_agent: str = "rebox-bot/0.1"
    env: str = "dev"
    extra: Mapping[str, str] = field(default_factory=dict)  # whole environment, for connectors

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        e = dict(os.environ if environ is None else environ)
        if not e.get("DATABASE_URL"):
            raise ConfigError(
                "DATABASE_URL is required (app-role URL, e.g. postgres://rebox_app:...@host/db)"
            )
        return cls(
            database_url=e["DATABASE_URL"],
            api_url=e.get("API_URL") or None,
            ingest_token=e.get("INGEST_TOKEN") or None,
            raw_store=e.get("RAW_STORE") or "file://./.data/raw",
            user_agent=e.get("USER_AGENT") or "rebox-bot/0.1",
            env=e.get("REBOX_ENV") or "dev",
            extra=e,
        )

    def require_writer(self) -> tuple[str, str]:
        if not self.api_url or not self.ingest_token:
            raise ConfigError("API_URL and INGEST_TOKEN are required to write claims")
        if not self.api_url.startswith(("http://", "https://")):
            raise ConfigError(f"API_URL must be an http(s) URL, got {self.api_url!r}")
        return self.api_url, self.ingest_token
