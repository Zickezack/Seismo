from pathlib import Path

import pytest

from app.config import AuthSettings, ConfigError, Settings

REQUIRED = {"POSTGRES_USER": "seismo", "POSTGRES_PASSWORD": "secret", "POSTGRES_DB": "seismo"}
AUTH_REQUIRED = {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "a" * 12, "SESSION_SECRET": "s" * 32}


def test_required_variables_only_gives_defaults():
    settings = Settings.from_env(REQUIRED)

    assert settings.postgres_host == "db"
    assert settings.postgres_port == 5432
    assert settings.log_level == "INFO"
    assert settings.worker_poll_interval_seconds == 5.0
    assert settings.backup_dir == Path("/backups")


def test_optional_variables_override_defaults():
    settings = Settings.from_env(
        REQUIRED
        | {
            "POSTGRES_HOST": "localhost",
            "POSTGRES_PORT": "15432",
            "LOG_LEVEL": "debug",
            "WORKER_POLL_INTERVAL_SECONDS": "0.5",
            "BACKUP_DIR": "/srv/backups",
        }
    )

    assert settings.postgres_host == "localhost"
    assert settings.postgres_port == 15432
    assert settings.log_level == "DEBUG"
    assert settings.worker_poll_interval_seconds == 0.5
    assert settings.backup_dir == Path("/srv/backups")


def test_missing_required_variables_are_all_named():
    with pytest.raises(ConfigError) as error:
        Settings.from_env({"POSTGRES_USER": "seismo", "POSTGRES_DB": "  "})

    assert "POSTGRES_PASSWORD" in str(error.value)
    assert "POSTGRES_DB" in str(error.value)
    assert "POSTGRES_USER" not in str(error.value)


def test_unknown_log_level_is_rejected():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env(REQUIRED | {"LOG_LEVEL": "verbose"})


@pytest.mark.parametrize("value", ["abc", "-7"])
def test_invalid_port_is_rejected_without_echoing_the_value(value):
    with pytest.raises(ConfigError, match="POSTGRES_PORT") as error:
        Settings.from_env(REQUIRED | {"POSTGRES_PORT": value})

    assert value not in str(error.value)


@pytest.mark.parametrize("value", ["0", "-1.5"])
def test_poll_interval_must_be_greater_than_zero(value):
    with pytest.raises(ConfigError, match="WORKER_POLL_INTERVAL_SECONDS must be greater than 0"):
        Settings.from_env(REQUIRED | {"WORKER_POLL_INTERVAL_SECONDS": value})


def test_password_is_hidden_in_repr():
    assert "secret" not in repr(Settings.from_env(REQUIRED))


def test_database_url_escapes_special_characters_in_the_password():
    settings = Settings.from_env(REQUIRED | {"POSTGRES_PASSWORD": "p@ss:w/rd"})

    url = settings.database_url()

    assert url.password == "p@ss:w/rd"
    assert url.host == "db"
    assert "p%40ss%3Aw%2Frd" in url.render_as_string(hide_password=False)


def test_auth_settings_need_no_database_variables():
    auth = AuthSettings.from_env(AUTH_REQUIRED | {"ADMIN_USERNAME": " admin "})

    assert auth.admin_username == "admin"
    assert auth.admin_password == "a" * 12
    assert auth.session_cookie_secure is False


def test_settings_need_no_admin_variables():
    # Worker and migrate load Settings only and never get the admin password (docker-compose.yml).
    Settings.from_env(REQUIRED)


def test_missing_auth_variables_are_all_named():
    with pytest.raises(ConfigError) as error:
        AuthSettings.from_env({"ADMIN_USERNAME": "admin", "SESSION_SECRET": " "})

    assert "ADMIN_PASSWORD" in str(error.value)
    assert "SESSION_SECRET" in str(error.value)
    assert "ADMIN_USERNAME" not in str(error.value)


def test_short_admin_password_is_rejected_without_echoing_it():
    with pytest.raises(ConfigError, match="ADMIN_PASSWORD must be at least 12 characters") as error:
        AuthSettings.from_env(AUTH_REQUIRED | {"ADMIN_PASSWORD": "change-me"})

    assert "change-me" not in str(error.value)


def test_short_session_secret_is_rejected():
    with pytest.raises(ConfigError, match="SESSION_SECRET must be at least 32 characters"):
        AuthSettings.from_env(AUTH_REQUIRED | {"SESSION_SECRET": "s" * 31})


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("TRUE", True), ("1", True), ("false", False), ("0", False)])
def test_session_cookie_secure_is_read(value, expected):
    auth = AuthSettings.from_env(AUTH_REQUIRED | {"SESSION_COOKIE_SECURE": value})

    assert auth.session_cookie_secure is expected


def test_invalid_session_cookie_secure_is_rejected():
    with pytest.raises(ConfigError, match="SESSION_COOKIE_SECURE must be true or false"):
        AuthSettings.from_env(AUTH_REQUIRED | {"SESSION_COOKIE_SECURE": "maybe"})


def test_admin_password_and_session_secret_are_hidden_in_repr():
    shown = repr(AuthSettings.from_env(AUTH_REQUIRED))

    assert "a" * 12 not in shown
    assert "s" * 32 not in shown
