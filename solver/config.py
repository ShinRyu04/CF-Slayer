"""Runtime configuration and first-run bearer token generation."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import suppress
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

TOKEN_ENV_VAR = "CF_SLAYER_AUTH_TOKEN"
DEFAULT_ENV_FILE = Path(".env")


class Settings(BaseSettings):
    """All fields map to ``CF_SLAYER_<FIELD>`` environment variables / .env keys."""

    model_config = SettingsConfigDict(env_prefix="CF_SLAYER_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8878
    ttl_seconds: int = 1800
    solve_timeout: float = 120.0
    challenge_timeout: float = 90.0
    poll_interval: float = 2.0
    replay_timeout: float = 30.0
    cache_path: str = "cache.sqlite3"
    headless: bool = True
    auth_token: str = ""

    # v2: ordered backend chain.
    backends: str = "camoufox,pydoll"
    chrome_path: str = ""

    @property
    def backend_names(self) -> list[str]:
        return [part.strip().lower() for part in self.backends.split(",") if part.strip()]


def ensure_token(env_file: Path = DEFAULT_ENV_FILE, settings: Settings | None = None) -> str:
    """Return the bearer token, generating one into ``.env`` on first run.

    Order: process environment > ``.env`` > generated.
    """
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token
    if settings is not None and settings.auth_token.strip():
        return settings.auth_token.strip()

    token = secrets.token_urlsafe(32)
    try:
        existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        env_file.write_text(
            f"{existing}{TOKEN_ENV_VAR}={token}\n", encoding="utf-8", newline="\n"
        )
        # POSIX mode is advisory on Windows; the ACL below is what actually matters.
        with suppress(OSError):
            os.chmod(env_file, 0o600)
        log.info("generated bearer token and wrote it to %s (chmod 600 requested)", env_file)
        log.info(
            "Windows does not enforce POSIX modes; restrict the file with: "
            'icacls .env /inheritance:r /grant:r "%%USERNAME%%:F"'
        )
    except OSError as exc:  # read-only FS, permission denied, ...
        log.warning("could not persist generated token to %s: %s", env_file, exc)
    return token


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    """Build settings, creating the token file when the token is absent."""
    settings = Settings(_env_file=env_file)
    settings.auth_token = ensure_token(env_file, settings)
    return settings
