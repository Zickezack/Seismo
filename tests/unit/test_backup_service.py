import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import make_url
from sqlalchemy.exc import ProgrammingError

from app.jobs import handlers
from app.jobs.errors import JobInterrupted
from app.repositories import backup_repository
from app.repositories.backup_repository import ToolFailed, ToolInterrupted
from app.services import backup_service, platform_settings_service
from app.services.backup_service import RestoreError
from app.services.platform_settings_service import PlatformSettings

NOW = datetime(2026, 10, 23, 3, 0, 5, tzinfo=UTC)
ENGINE = SimpleNamespace(url=make_url("postgresql+psycopg://seismo:secret@db:5432/seismo"))


def touch(directory, name):
    path = directory / name
    path.write_bytes(b"dump")
    return path


@pytest.fixture
def keep(monkeypatch):
    """Set the number of backups to keep (Plattform-Einstellungen) without a database."""

    def use(number):
        settings = PlatformSettings(backups_to_keep=number)
        monkeypatch.setattr(platform_settings_service, "current", lambda _engine: settings)

    use(14)
    return use


@pytest.fixture
def fake_dump(monkeypatch):
    """Replace pg_dump: writes a small file, or raises the given error."""
    calls = []

    def use(error=None):
        def dump(url, target, stop_event):
            calls.append((url.database, target.name))
            target.write_bytes(b"PGDMP")
            if error:
                raise error

        monkeypatch.setattr(backup_repository, "dump", dump)
        return calls

    return use


def test_backup_name_carries_the_database_and_the_utc_time():
    name = backup_service.backup_name("seismo", NOW)

    assert name == "seismo-20261023T030005Z.dump"
    assert backup_service.created_at_of("seismo", name) == NOW


@pytest.mark.parametrize(
    "name",
    ["seismo_test-20261023T030005Z.dump", "seismo-latest.dump", "seismo-20261023T030005Z.dump.partial", "notes.txt"],
)
def test_files_of_other_databases_and_foreign_files_are_no_backups(name):
    assert backup_service.created_at_of("seismo", name) is None


def test_list_backups_newest_first_without_partial_or_foreign_files(tmp_path):
    for hours in (0, 2, 1):
        touch(tmp_path, backup_service.backup_name("seismo", NOW + timedelta(hours=hours)))
    touch(tmp_path, "seismo-20261024T000000Z.dump.partial")
    touch(tmp_path, "seismo_test-20261024T000000Z.dump")

    backups = backup_service.list_backups(tmp_path, "seismo")

    assert [backup.created_at for backup in backups] == [NOW + timedelta(hours=2), NOW + timedelta(hours=1), NOW]
    assert backups[0].size_bytes == 4


def test_a_missing_backup_directory_has_no_backups(tmp_path):
    assert backup_service.list_backups(tmp_path / "missing", "seismo") == []


@pytest.mark.parametrize(
    ("age", "due"),
    [(None, True), (timedelta(hours=23, minutes=59), False), (timedelta(hours=24), True), (timedelta(days=3), True)],
)
def test_a_backup_is_due_once_the_newest_is_as_old_as_the_interval(tmp_path, age, due):
    if age is not None:
        touch(tmp_path, backup_service.backup_name("seismo", NOW - age))

    assert backup_service.is_due(backup_service.list_backups(tmp_path, "seismo"), 24, NOW) is due


def test_create_backup_renames_the_complete_dump_and_leaves_no_partial_file(tmp_path, keep, fake_dump):
    calls = fake_dump()

    backup = backup_service.create_backup(ENGINE, tmp_path, now=NOW)

    assert calls == [("seismo", "seismo-20261023T030005Z.dump.partial")]
    assert [path.name for path in tmp_path.iterdir()] == ["seismo-20261023T030005Z.dump"]
    assert (backup.path, backup.created_at, backup.size_bytes) == (tmp_path / "seismo-20261023T030005Z.dump", NOW, 5)


@pytest.mark.parametrize("error", [ToolFailed("pg_dump mit Exit-Code 1 beendet"), ToolInterrupted("Worker stoppt")])
def test_a_failed_or_interrupted_dump_leaves_no_file(tmp_path, keep, fake_dump, error):
    fake_dump(error)

    with pytest.raises(type(error)):
        backup_service.create_backup(ENGINE, tmp_path, now=NOW)

    assert list(tmp_path.iterdir()) == []


def test_a_backup_counts_even_when_old_ones_cannot_be_deleted(tmp_path, fake_dump, monkeypatch, caplog):
    def schema_behind_the_code(_engine):
        raise ProgrammingError("SELECT", {}, Exception("column platform_settings.backups_to_keep does not exist"))

    monkeypatch.setattr(platform_settings_service, "current", schema_behind_the_code)
    fake_dump()

    backup = backup_service.create_backup(ENGINE, tmp_path, now=NOW)

    assert backup.path.exists()
    assert "Deleting old backups skipped: column platform_settings.backups_to_keep does not exist" in caplog.text


def test_create_backup_names_a_missing_backup_directory(tmp_path, keep, fake_dump):
    calls = fake_dump()

    with pytest.raises(FileNotFoundError, match="Backup-Verzeichnis .*missing fehlt"):
        backup_service.create_backup(ENGINE, tmp_path / "missing", now=NOW)

    assert calls == []


def test_only_the_newest_backups_are_kept_and_foreign_files_stay(tmp_path, keep, fake_dump):
    keep(2)
    fake_dump()
    for hours in (3, 2, 1):
        touch(tmp_path, backup_service.backup_name("seismo", NOW - timedelta(hours=hours)))
    foreign = [touch(tmp_path, "seismo_test-20200101T000000Z.dump"), touch(tmp_path, "notes.txt")]

    backup_service.create_backup(ENGINE, tmp_path, now=NOW)

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == sorted(["seismo-20261023T030005Z.dump", "seismo-20261023T020005Z.dump"] + [p.name for p in foreign])


def test_partial_dumps_older_than_the_newest_backup_are_deleted_newer_ones_stay(tmp_path, keep):
    touch(tmp_path, backup_service.backup_name("seismo", NOW))
    touch(tmp_path, "seismo-20261023T010000Z.dump.partial")  # broke off before the newest backup
    touch(tmp_path, "seismo-20261023T040000Z.dump.partial")  # may still be running

    backup_service.delete_old_backups(ENGINE, tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "seismo-20261023T030005Z.dump",
        "seismo-20261023T040000Z.dump.partial",
    ]


def test_restore_rejects_a_missing_file_before_touching_any_database(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_repository, "maintenance_engine", lambda _url: pytest.fail("no database access"))

    with pytest.raises(RestoreError, match="nicht gefunden"):
        backup_service.restore(ENGINE, tmp_path / "missing.dump")


def test_restore_rejects_a_database_name_too_long_to_keep_the_replaced_one(tmp_path):
    # 40 + len("_before_restore_20261023030005") = 70 bytes; PostgreSQL keeps 63.
    engine = SimpleNamespace(url=ENGINE.url.set(database="s" * 40))

    with pytest.raises(RestoreError, match="zu lang"):
        backup_service.restore(engine, touch(tmp_path, "x.dump"))


def test_backup_job_goes_back_into_the_queue_when_the_worker_stops(monkeypatch, tmp_path):
    def interrupted(_engine, _backup_dir, _stop_event):
        raise ToolInterrupted("pg_dump beendet, weil der Worker stoppt")

    monkeypatch.setattr(backup_service, "create_backup", interrupted)

    with pytest.raises(JobInterrupted):
        handlers.run_backup_job(ENGINE, tmp_path, threading.Event())
