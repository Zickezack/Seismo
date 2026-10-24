"""Settings from environment variables - the only configuration source (architecture.md)."""
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import URL

REQUIRED_VARIABLES = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
AUTH_VARIABLES = ("ADMIN_USERNAME", "ADMIN_PASSWORD", "SESSION_SECRET")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_ADMIN_PASSWORD_LENGTH = 12
MIN_SESSION_SECRET_LENGTH = 32
# The extraction's instructions and the room for its answer take 4000 tokens (extraction_service.RESERVED_TOKENS);
# this leaves about 2000 for the article's text.
MIN_OLLAMA_NUM_CTX = 6144


class ConfigError(ValueError):
    """Raised for missing or invalid environment variables. Names the variable, never its value."""


@dataclass(frozen=True)
class Settings:
    postgres_user: str
    postgres_password: str = field(repr=False)
    postgres_db: str
    postgres_host: str = "db"
    postgres_port: int = 5432
    log_level: str = "INFO"
    worker_poll_interval_seconds: float = 5.0
    # The worker's backup volume (docker-compose.yml); outside it a backup would not survive a redeploy.
    backup_dir: Path = Path("/backups")
    # How a source can reach the operator (e-mail or URL), sent in the User-Agent of every fetch.
    fetch_contact: str = ""
    # The LLM server (architecture.md, "Environments and LLM"); empty: no extraction is queued.
    ollama_base_url: str = ""
    ollama_model: str = "qwen3.5:9b"
    # The context the model runs with, in tokens: the article's text is cut to fit it.
    ollama_num_ctx: int = 8192

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        environ = os.environ if environ is None else environ
        _require(environ, REQUIRED_VARIABLES)

        log_level = environ.get("LOG_LEVEL", cls.log_level).strip().upper()
        if log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")

        return cls(
            postgres_user=environ["POSTGRES_USER"].strip(),
            postgres_password=environ["POSTGRES_PASSWORD"],
            postgres_db=environ["POSTGRES_DB"].strip(),
            postgres_host=environ.get("POSTGRES_HOST", cls.postgres_host).strip(),
            postgres_port=_positive_number(environ, "POSTGRES_PORT", cls.postgres_port, int),
            log_level=log_level,
            worker_poll_interval_seconds=_positive_number(
                environ, "WORKER_POLL_INTERVAL_SECONDS", cls.worker_poll_interval_seconds, float
            ),
            backup_dir=Path(environ.get("BACKUP_DIR", "").strip() or cls.backup_dir),
            fetch_contact=_single_line(environ, "FETCH_CONTACT"),
            ollama_base_url=_base_url(environ, "OLLAMA_BASE_URL"),
            ollama_model=_single_line(environ, "OLLAMA_MODEL") or cls.ollama_model,
            ollama_num_ctx=_ollama_num_ctx(environ),
        )

    @property
    def log_level_number(self) -> int:
        return logging.getLevelNamesMapping()[self.log_level]

    def database_url(self) -> URL:
        # URL.create escapes special characters in the password, unlike string formatting.
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@dataclass(frozen=True)
class AuthSettings:
    """Admin login of the api. Kept apart from Settings, so worker and migrate never get the password."""

    admin_username: str
    admin_password: str = field(repr=False)
    session_secret: str = field(repr=False)
    # Only true once the api is served over HTTPS; browsers never send a Secure cookie over plain HTTP.
    session_cookie_secure: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AuthSettings":
        environ = os.environ if environ is None else environ
        _require(environ, AUTH_VARIABLES)

        # The placeholders in .env.example are shorter than these minimums, so a copied template never starts.
        if len(environ["ADMIN_PASSWORD"]) < MIN_ADMIN_PASSWORD_LENGTH:
            raise ConfigError(f"ADMIN_PASSWORD must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters long")
        session_secret = environ["SESSION_SECRET"].strip()
        if len(session_secret) < MIN_SESSION_SECRET_LENGTH:
            raise ConfigError(f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters long")

        return cls(
            admin_username=environ["ADMIN_USERNAME"].strip(),
            admin_password=environ["ADMIN_PASSWORD"],
            session_secret=session_secret,
            session_cookie_secure=_boolean(environ, "SESSION_COOKIE_SECURE", cls.session_cookie_secure),
        )


def _require(environ, names):
    missing = [name for name in names if not environ.get(name, "").strip()]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")


def _single_line(environ, name):
    value = environ.get(name, "").strip()
    # It goes into an HTTP header, where a line break would start a header of its own.
    if any(char in value for char in "\r\n"):
        raise ConfigError(f"{name} must be a single line")
    return value


def _base_url(environ, name):
    value = _single_line(environ, name).rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must start with http:// or https://")
    return value


def _ollama_num_ctx(environ):
    value = _positive_number(environ, "OLLAMA_NUM_CTX", Settings.ollama_num_ctx, int)
    if value < MIN_OLLAMA_NUM_CTX:
        raise ConfigError(f"OLLAMA_NUM_CTX must be at least {MIN_OLLAMA_NUM_CTX}")
    return value


def _boolean(environ, name, default):
    raw = environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _positive_number(environ, name, default, number_type):
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = number_type(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0")
    return value
