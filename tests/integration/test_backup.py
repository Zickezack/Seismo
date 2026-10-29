"""Backups with the real pg_dump and pg_restore: a restore brings the data back and never loses the
database it replaces. Needs scripts/install-postgres-client.sh (in the dev container: on every build)."""
import shutil
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text

from app.db.models import Job
from app.repositories import backup_repository
from app.repositories.backup_repository import ToolFailed
from app.services import backup_service, job_service, platform_settings_service
from app.services.backup_service import RestoreError
from tests.integration.alembic_helpers import upgrade

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)

if shutil.which("pg_dump") is None:
    pytest.fail("pg_dump fehlt: Dev-Container neu bauen oder scripts/install-postgres-client.sh ausführen", pytrace=False)


@pytest.fixture
def database(engine, test_settings):
    upgrade(engine)
    yield engine
    # A restore keeps the replaced database; the test session drops only the test database itself.
    with backup_repository.maintenance_engine(engine.url).connect() as connection:
        names = connection.execute(
            text("SELECT datname FROM pg_database WHERE datname LIKE :pattern"),
            {"pattern": f"{test_settings.postgres_db}\\_%"},
        ).scalars()
        for name in list(names):
            backup_repository.drop_database(connection, name)


@pytest.fixture
def backup_dir(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    return directory


def job_count(engine):
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(Job)).scalar_one()


def databases(engine, test_settings):
    with backup_repository.maintenance_engine(engine.url).connect() as connection:
        return set(
            connection.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE :pattern"), {"pattern": f"{test_settings.postgres_db}%"}
            ).scalars()
        )


def test_restore_brings_back_the_backed_up_data_and_keeps_the_replaced_database(database, backup_dir, test_settings):
    job_service.enqueue_test_job(database)
    platform_settings_service.save(database, {"llm_max_parallel_calls": "3"})
    backup = backup_service.create_backup(database, backup_dir, now=NOW)
    # Changes after the backup: lost by the restore, but still in the replaced database.
    job_service.enqueue_test_job(database)
    platform_settings_service.save(database, {"llm_max_parallel_calls": "5"})

    result = backup_service.restore(database, backup.path, now=NOW + timedelta(hours=1))

    assert job_count(database) == 1
    assert platform_settings_service.current(database).llm_max_parallel_calls == 3
    assert result.previous_database == f"{test_settings.postgres_db}_before_restore_20261023040000"
    replaced = create_engine(database.url.set(database=result.previous_database))
    try:
        assert job_count(replaced) == 2
    finally:
        replaced.dispose()
    assert databases(database, test_settings) == {test_settings.postgres_db, result.previous_database}


def test_a_file_without_a_seismo_database_changes_nothing(database, backup_dir, test_settings):
    job_service.enqueue_test_job(database)
    other = backup_dir / "other.dump"
    # A database without Alembic version table, dumped like a backup.
    with backup_repository.maintenance_engine(database.url).connect() as connection:
        backup_repository.create_database(connection, f"{test_settings.postgres_db}_other")
    backup_repository.dump(database.url.set(database=f"{test_settings.postgres_db}_other"), other, threading.Event())

    with pytest.raises(RestoreError, match="keine Seismo-Datenbank"):
        backup_service.restore(database, other)

    assert job_count(database) == 1
    assert f"{test_settings.postgres_db}_restore" not in databases(database, test_settings)


def test_a_damaged_file_changes_nothing(database, backup_dir, test_settings):
    job_service.enqueue_test_job(database)
    damaged = backup_dir / "damaged.dump"
    damaged.write_bytes(b"PGDMP but cut off")

    with pytest.raises(ToolFailed, match="pg_restore mit Exit-Code"):
        backup_service.restore(database, damaged)

    assert job_count(database) == 1
    assert databases(database, test_settings) == {test_settings.postgres_db}


def test_a_failed_dump_leaves_no_file(database, backup_dir):
    wrong_password = create_engine(database.url.set(password="wrong-password"))
    try:
        with pytest.raises(ToolFailed, match="pg_dump mit Exit-Code"):
            backup_service.create_backup(wrong_password, backup_dir, now=NOW)
    finally:
        wrong_password.dispose()

    assert list(backup_dir.iterdir()) == []


def test_old_backups_beyond_the_number_to_keep_are_deleted(database, backup_dir, test_settings):
    platform_settings_service.save(database, {"backups_to_keep": "2"})

    for hours in range(3):
        backup_service.create_backup(database, backup_dir, now=NOW + timedelta(hours=hours))

    kept = backup_service.list_backups(backup_dir, test_settings.postgres_db)
    assert [backup.created_at for backup in kept] == [NOW + timedelta(hours=2), NOW + timedelta(hours=1)]


def test_a_backup_job_is_queued_once_while_one_is_due(database, backup_dir):
    first = backup_service.enqueue_if_due(database, backup_dir, now=NOW)
    second = backup_service.enqueue_if_due(database, backup_dir, now=NOW)

    assert first is not None
    assert second is None
    assert job_count(database) == 1


def test_a_backup_now_is_queued_besides_the_schedule_but_never_twice(database, backup_dir, test_settings):
    (backup_dir / backup_service.backup_name(test_settings.postgres_db, NOW)).touch()

    assert backup_service.enqueue_now(database) is not None
    assert backup_service.enqueue_now(database) is None
    assert backup_service.enqueue_if_due(database, backup_dir, now=NOW + timedelta(days=2)) is None
    assert job_count(database) == 1


def test_no_backup_job_while_the_newest_backup_is_younger_than_the_interval(database, backup_dir, test_settings):
    platform_settings_service.save(database, {"backup_interval_hours": "6"})
    (backup_dir / backup_service.backup_name(test_settings.postgres_db, NOW)).touch()

    assert backup_service.enqueue_if_due(database, backup_dir, now=NOW + timedelta(hours=5, minutes=59)) is None
    assert backup_service.enqueue_if_due(database, backup_dir, now=NOW + timedelta(hours=6)) is not None


def test_restore_ends_sessions_still_connected_to_the_database(database, backup_dir):
    backup = backup_service.create_backup(database, backup_dir, now=NOW)
    # Like an api or worker that was not stopped.
    other = create_engine(database.url)
    connection = other.connect()
    try:
        backup_service.restore(database, backup.path, now=NOW + timedelta(hours=1))

        with pytest.raises(Exception, match="terminat"):
            connection.execute(text("SELECT 1"))
    finally:
        connection.invalidate()
        other.dispose()
    assert job_count(database) == 0
