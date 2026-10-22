"""Settings from environment variables - the only configuration source (architecture.md)."""
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import URL

REQUIRED_VARIABLES = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


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

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        environ = os.environ if environ is None else environ
        missing = [name for name in REQUIRED_VARIABLES if not environ.get(name, "").strip()]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

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
