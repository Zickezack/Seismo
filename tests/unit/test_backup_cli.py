from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import make_url

from app.backup import cli
from app.config import Settings
from app.repositories.backup_repository import ToolFailed
from app.services import backup_service
from app.services.backup_service import BackupFile, RestoreResult

BACKUP_DIR = Path("/backups")


class FakeEngine:
    url = make_url("postgresql+psycopg://seismo:secret@db:5432/seismo")

    def dispose(self):
        pass


@pytest.fixture
def restores(monkeypatch):
    """Run the commands without a database; collects the files restore() was called with."""
    settings = Settings(postgres_user="seismo", postgres_password="secret", postgres_db="seismo", backup_dir=BACKUP_DIR)
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "create_db_engine", lambda _settings: FakeEngine())
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "head_revision", lambda: "head")
    calls = []

    def restore(_engine, source):
        calls.append(source)
        return RestoreResult("head", "seismo_before_restore_20261023030000")

    monkeypatch.setattr(backup_service, "restore", restore)
    return calls


@pytest.mark.parametrize("answer", ["", "ja", "seismo_test"])
def test_restore_without_the_database_name_changes_nothing(restores, monkeypatch, capsys, answer):
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert cli.main(["restore", "seismo-20261023T030000Z.dump"]) == 1
    assert restores == []
    assert "nichts geändert" in capsys.readouterr().out


def test_restore_without_a_terminal_changes_nothing(restores, monkeypatch):
    def no_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)

    assert cli.main(["restore", "seismo-20261023T030000Z.dump"]) == 1
    assert restores == []


def test_restore_confirmed_with_the_database_name_uses_the_file_from_the_backup_directory(restores, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: " seismo ")

    assert cli.main(["restore", "seismo-20261023T030000Z.dump"]) == 0
    assert restores == [BACKUP_DIR / "seismo-20261023T030000Z.dump"]
    assert '"seismo_before_restore_20261023030000"' in capsys.readouterr().out


def test_restore_with_yes_takes_an_absolute_path_as_given(restores):
    assert cli.main(["restore", "/mnt/nas/seismo-20261023T030000Z.dump", "--yes"]) == 0
    assert restores == [Path("/mnt/nas/seismo-20261023T030000Z.dump")]


def test_a_failure_is_printed_without_a_traceback(restores, monkeypatch, capsys):
    def failing_create(_engine, _backup_dir):
        raise ToolFailed("pg_dump mit Exit-Code 1 beendet: connection refused")

    monkeypatch.setattr(backup_service, "create_backup", failing_create)

    assert cli.main(["create"]) == 1
    assert capsys.readouterr().err == "Fehler: pg_dump mit Exit-Code 1 beendet: connection refused\n"


def test_list_shows_name_size_and_time(restores, monkeypatch, capsys):
    backup = BackupFile(BACKUP_DIR / "seismo-20261023T030000Z.dump", datetime(2026, 10, 23, 3, tzinfo=UTC), 5 * 1024 * 1024)
    monkeypatch.setattr(backup_service, "list_backups", lambda _directory, _database: [backup])

    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == "seismo-20261023T030000Z.dump      5.0 MB  2026-10-23 03:00:00 UTC\n"
