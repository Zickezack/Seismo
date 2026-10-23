"""Database backups: when one is due, creating one, which files to keep, restoring one.

A backup is a pg_dump file `<database>-<UTC time>.dump` in the backup directory (Settings.backup_dir).
The newest file is the schedule: a backup is due once it is older than the backup interval
(Plattform-Einstellungen), so a manual backup counts as well and a new interval applies at once.
"""
import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.repositories import backup_repository
from app.services import job_service, platform_settings_service

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".dump"
# pg_dump writes here first; only a complete dump gets the backup name.
PARTIAL_SUFFIX = ".dump.partial"
TIME_FORMAT = "%Y%m%dT%H%M%SZ"
# Exactly the shape TIME_FORMAT writes: strptime alone would read a shortened "T1415Z" as 14:01:05.
_TIME_SHAPE = re.compile(r"\d{8}T\d{6}Z")
RESTORE_SUFFIX = "_restore"
PREVIOUS_SUFFIX = "_before_restore_"
# PostgreSQL cuts longer database names, which could make two of them equal.
MAX_DATABASE_NAME_BYTES = 63
SWAP_ATTEMPTS = 3


class RestoreError(Exception):
    """The restore cannot start or did not finish; the database is unchanged. Shown to the admin as is."""


@dataclass(frozen=True)
class BackupFile:
    path: Path
    created_at: datetime  # UTC, from the file name
    size_bytes: int


@dataclass(frozen=True)
class RestoreResult:
    revision: str  # the Alembic revision of the restored database
    previous_database: str | None  # where the replaced database is kept, None if there was none


def backup_name(database: str, created_at: datetime) -> str:
    return f"{database}-{created_at.astimezone(UTC).strftime(TIME_FORMAT)}{BACKUP_SUFFIX}"


def created_at_of(database: str, name: str, suffix: str = BACKUP_SUFFIX) -> datetime | None:
    """The time in a backup file name of this database; None for any other file, which is never touched."""
    prefix = f"{database}-"
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    stamp = name[len(prefix) : -len(suffix)]
    if not _TIME_SHAPE.fullmatch(stamp):
        return None
    try:
        return datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def list_backups(backup_dir: Path, database: str) -> list[BackupFile]:
    """The complete backups of this database, newest first."""
    backups = []
    for path in backup_repository.files(backup_dir, BACKUP_SUFFIX):
        created_at = created_at_of(database, path.name)
        if created_at is not None:
            backups.append(BackupFile(path, created_at, path.stat().st_size))
    return sorted(backups, key=lambda backup: backup.created_at, reverse=True)


def is_due(backups: list[BackupFile], interval_hours: int, now: datetime) -> bool:
    return not backups or now - backups[0].created_at >= timedelta(hours=interval_hours)


def enqueue_if_due(engine: Engine, backup_dir: Path, now: datetime | None = None) -> int | None:
    """Queue a backup job once the newest backup is older than the interval. The dedupe key keeps it to
    one waiting or running backup job, however many workers check. Returns the new job's id or None."""
    now = now or datetime.now(UTC)
    database = engine.url.database
    with engine.begin() as connection:
        interval_hours = platform_settings_service.load(connection).backup_interval_hours
        if not is_due(list_backups(backup_dir, database), interval_hours, now):
            return None
        return job_service.enqueue(
            connection,
            job_service.BACKUP_JOB_KIND,
            dedupe_key=job_service.BACKUP_JOB_KIND,
            max_attempts=job_service.BACKUP_MAX_ATTEMPTS,
        )


def create_backup(
    engine: Engine, backup_dir: Path, stop_event: threading.Event | None = None, now: datetime | None = None
) -> BackupFile:
    """Dump the database into a new backup file, then delete the oldest beyond the number to keep."""
    now = now or datetime.now(UTC)
    database = engine.url.database
    target = backup_dir / backup_name(database, now)
    partial = target.with_name(target.name.removesuffix(BACKUP_SUFFIX) + PARTIAL_SUFFIX)
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"Backup-Verzeichnis {backup_dir} fehlt (BACKUP_DIR, README.md)")
    try:
        backup_repository.dump(engine.url, partial, stop_event or threading.Event())
        partial.rename(target)
    finally:
        backup_repository.delete([partial])
    backup = BackupFile(target, now, target.stat().st_size)
    logger.info("Backup created", extra={"file": target.name, "size_bytes": backup.size_bytes})
    try:
        delete_old_backups(engine, backup_dir)
    except SQLAlchemyError as error:
        # E.g. a schema older than the code has no backup settings yet. The backup is complete all the
        # same; the next one deletes the old files.
        logger.warning("Deleting old backups skipped: %s", getattr(error, "orig", None) or error)
    return backup


def delete_old_backups(engine: Engine, backup_dir: Path) -> list[BackupFile]:
    """Delete the backups beyond the number to keep, and partial dumps older than the newest backup -
    those can no longer finish. Returns the deleted backups."""
    database = engine.url.database
    backups = list_backups(backup_dir, database)
    old = backups[platform_settings_service.current(engine).backups_to_keep :]
    stale = []
    if backups:
        for path in backup_repository.files(backup_dir, PARTIAL_SUFFIX):
            started_at = created_at_of(database, path.name, PARTIAL_SUFFIX)
            if started_at is not None and started_at < backups[0].created_at:
                stale.append(path)
    backup_repository.delete([backup.path for backup in old] + stale)
    if old:
        logger.info("Old backups deleted", extra={"files": [backup.path.name for backup in old]})
    return old


def restore(engine: Engine, source: Path, now: datetime | None = None) -> RestoreResult:
    """Replace the database with the backup `source`, keeping the replaced database under a new name.

    The backup is loaded into a database of its own first; only a complete load replaces the current
    database, in one rename transaction. Until then the current database stays untouched.
    """
    now = now or datetime.now(UTC)
    database = engine.url.database
    staging = f"{database}{RESTORE_SUFFIX}"
    previous = f"{database}{PREVIOUS_SUFFIX}{now.astimezone(UTC).strftime('%Y%m%d%H%M%S')}"
    if len(previous.encode()) > MAX_DATABASE_NAME_BYTES:
        raise RestoreError(f"Datenbankname {database} ist zu lang, um die bisherige Datenbank aufzubewahren")
    if not source.is_file():
        raise RestoreError(f"Backup-Datei {source} nicht gefunden")

    maintenance = backup_repository.maintenance_engine(engine.url)
    # The engine's pool would keep a connection to the database that gets renamed.
    engine.dispose()
    try:
        with maintenance.connect() as connection:
            # Left over from a restore that broke off; it never held the live data.
            backup_repository.drop_database(connection, staging)
            backup_repository.create_database(connection, staging)
            existed = backup_repository.database_exists(connection, database)
        try:
            backup_repository.restore(engine.url.set(database=staging), source, threading.Event())
            revision = backup_repository.revision_of(engine.url.set(database=staging))
            if revision is None:
                raise RestoreError("Die Datei enthält keine Seismo-Datenbank (keine Alembic-Version)")
            _swap(maintenance, database, staging, previous if existed else None)
        except BaseException:
            with maintenance.connect() as connection:
                backup_repository.drop_database(connection, staging)
            raise
    finally:
        maintenance.dispose()
    logger.info("Database restored", extra={"file": source.name, "revision": revision})
    return RestoreResult(revision, previous if existed else None)


def _swap(maintenance: Engine, database: str, staging: str, previous: str | None) -> None:
    # A running api or worker reconnects within seconds; one of a few quick attempts gets through.
    for attempt in range(1, SWAP_ATTEMPTS + 1):
        try:
            with maintenance.connect() as connection:
                backup_repository.swap_databases(connection, database, staging, previous)
            return
        except backup_repository.DatabaseInUse:
            if attempt == SWAP_ATTEMPTS:
                raise RestoreError(
                    f"Datenbank {database} ist noch in Benutzung - api und worker stoppen, dann erneut versuchen"
                ) from None
