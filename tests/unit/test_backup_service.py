import io
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
from app.services.backup_service import InvalidBackup, RestoreError
from app.services.bulk import BulkResult
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
    [
        "seismo_test-20261023T030005Z.dump",
        "seismo-latest.dump",
        "seismo-20261023T030005Z.dump.partial",
        "notes.txt",
        # A shortened time is no backup of ours, so rotation never deletes it.
        "seismo-20261023T0305Z.dump",
    ],
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


def test_the_list_shows_backups_made_here_and_uploaded_ones_newest_first_and_nothing_else(tmp_path):
    touch(tmp_path, "seismo-20261023T030000Z.dump")
    touch(tmp_path, "seismo-upload-20261024T120000Z.dump")
    touch(tmp_path, "seismo-20261022T030000Z.dump")
    for other in ("seismo-upload-20261025T120000Z.dump.uploading", "other-20261025T030000Z.dump", "notes.txt"):
        touch(tmp_path, other)

    stored = backup_service.stored_backups(tmp_path, "seismo")

    assert [(backup.name, backup.uploaded) for backup in stored] == [
        ("seismo-upload-20261024T120000Z.dump", True),
        ("seismo-20261023T030000Z.dump", False),
        ("seismo-20261022T030000Z.dump", False),
    ]
    assert stored[0].created_at == datetime(2026, 10, 24, 12, tzinfo=UTC) and stored[0].size_bytes == 4


def test_an_uploaded_backup_is_kept_under_its_own_name_and_never_counts_for_the_schedule(tmp_path):
    touch(tmp_path, "seismo-20261020T030000Z.dump")

    backup = backup_service.store_upload(tmp_path, "seismo", io.BytesIO(b"PGDMP\x01\x0e rest of the dump"), NOW)

    assert backup.name == "seismo-upload-20261023T030005Z.dump" and backup.uploaded
    assert (tmp_path / backup.name).read_bytes() == b"PGDMP\x01\x0e rest of the dump"
    assert [found.path.name for found in backup_service.list_backups(tmp_path, "seismo")] == [
        "seismo-20261020T030000Z.dump"
    ]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["seismo-20261020T030000Z.dump", backup.name]


@pytest.mark.parametrize("content", [b"", b"SQLite format 3\x00", b"-- PostgreSQL database dump\n"])
def test_an_upload_that_is_no_pg_dump_file_is_not_stored(tmp_path, content):
    with pytest.raises(InvalidBackup, match="kein Backup von pg_dump"):
        backup_service.store_upload(tmp_path, "seismo", io.BytesIO(content), NOW)

    assert list(tmp_path.iterdir()) == []


def test_an_upload_without_the_backup_directory_is_refused(tmp_path):
    with pytest.raises(InvalidBackup, match="Backup-Verzeichnis"):
        backup_service.store_upload(tmp_path / "missing", "seismo", io.BytesIO(b"PGDMP"), NOW)


GB = 1024 ** 3


def test_an_upload_that_would_leave_too_little_free_is_refused_before_copying(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_service.backup_repository, "free_bytes", lambda _directory: 2 * GB + 10)

    with pytest.raises(InvalidBackup) as refused:
        backup_service.store_upload(tmp_path, "seismo", io.BytesIO(b"PGDMP" + b"x" * 20), NOW)

    assert str(refused.value) == "Zu wenig Platz: die Datei (0,0 GB) liesse weniger als 2,0 GB auf dem Backup-Speicher frei (2,0 GB frei)."
    assert list(tmp_path.iterdir()) == []


def test_an_upload_stops_once_the_space_runs_short_while_copying_and_leaves_no_part(tmp_path, monkeypatch):
    # Enough before copying; then something else takes the space.
    free = iter([3 * GB, 3 * GB, GB, GB])
    monkeypatch.setattr(backup_service.backup_repository, "free_bytes", lambda _directory: next(free))
    monkeypatch.setattr(backup_service.backup_repository, "COPY_CHUNK_BYTES", 4)

    with pytest.raises(InvalidBackup, match=r"^Zu wenig Platz: .*\(1,0 GB frei\)\.$"):
        backup_service.store_upload(tmp_path, "seismo", io.BytesIO(b"PGDMP" + b"x" * 20), NOW)

    assert list(tmp_path.iterdir()) == []


def test_only_a_listed_backup_is_served(tmp_path):
    touch(tmp_path, "seismo-20261023T030000Z.dump")
    touch(tmp_path, "notes.txt")

    assert backup_service.stored_path(tmp_path, "seismo", "seismo-20261023T030000Z.dump") == (
        tmp_path / "seismo-20261023T030000Z.dump"
    )
    for name in ("notes.txt", "../seismo-20261023T030000Z.dump", "seismo-20261024T030000Z.dump"):
        assert backup_service.stored_path(tmp_path, "seismo", name) is None


def test_deleting_backups_is_all_or_nothing(tmp_path):
    made = touch(tmp_path, "seismo-20261023T030000Z.dump")
    uploaded = touch(tmp_path, "seismo-upload-20261024T120000Z.dump")
    notes = touch(tmp_path, "notes.txt")

    result = backup_service.delete_stored(tmp_path, "seismo", [made.name, "notes.txt"])

    assert result == BulkResult(processed=0, blocked=["notes.txt (nicht gefunden)"])
    assert made.exists() and notes.exists()

    assert backup_service.delete_stored(tmp_path, "seismo", [made.name, uploaded.name, made.name]) == BulkResult(2, [])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["notes.txt"]
