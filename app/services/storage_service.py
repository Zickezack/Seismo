"""Storage monitoring (userstories.md, Verdichtung und Daten-Lifecycle, story 2; requirements.md, Verdichtung und
Daten-Lifecycle, criterion 4): what the data takes now, and a measurement once a day, so its growth is known before a
disk fills and hardware decisions rest on measured values (architecture.md, "Stages and hardware sizing").

Measured from PostgreSQL's catalogue (database and tables, with their indexes and TOAST), the raw texts as stored -
compressed - and the backup store's files and free space. The database volume's own free space is not visible from
here: with the backups on the same disk, the backup store's comes closest.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import Connection, Engine

from app.repositories import backup_repository, storage_repository
from app.services import backup_service

logger = logging.getLogger(__name__)

# The days of measurements the page shows.
HISTORY_LIMIT = 60
# The periods the page shows the growth over, in days.
GROWTH_PERIODS = (7, 30)


@dataclass(frozen=True)
class TableSize:
    name: str
    size_bytes: int
    share: float  # of the database, 0 to 1


@dataclass(frozen=True)
class SourceRawTexts:
    source_id: int | None  # None once the Quelle is deleted
    name: str
    count: int
    size_bytes: int


@dataclass(frozen=True)
class Storage:
    """What the data takes now."""

    database_bytes: int
    tables: list[TableSize]  # the largest first
    article_count: int
    raw_text_count: int
    raw_text_bytes: int
    backup_count: int
    backup_bytes: int
    backup_free_bytes: int | None  # None when it could not be read
    by_source: list[SourceRawTexts]  # raw texts per Quelle, the largest first


@dataclass(frozen=True)
class Measurement:
    measured_on: date
    database_bytes: int
    article_count: int
    raw_text_count: int
    raw_text_bytes: int
    backup_bytes: int
    backup_free_bytes: int | None


@dataclass(frozen=True)
class Growth:
    """Per day, from the measurement `days` before the latest - or the earliest, while there is none that old."""

    period: int  # the period asked for, in days
    days: int  # the days it is measured over
    database_bytes: float
    raw_text_bytes: float
    articles: float


def current(engine: Engine, backup_dir: Path) -> Storage:
    with engine.connect() as connection:
        values = _measure(connection, backup_dir)
        by_source = storage_repository.raw_texts_by_source(connection)
    total = values["database_bytes"] or 1
    return Storage(
        values["database_bytes"],
        [TableSize(name, size, size / total) for name, size in values["table_bytes"].items()],
        values["article_count"], values["raw_text_count"], values["raw_text_bytes"], values["backup_count"],
        values["backup_bytes"], values["backup_free_bytes"],
        [SourceRawTexts(row.id, row.name, row.count, int(row.size_bytes)) for row in by_source],
    )


def measure_if_due(engine: Engine, backup_dir: Path, now: datetime | None = None) -> bool:
    """Measure once a UTC day; whether it measured now. The worker calls it once an hour."""
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    with engine.begin() as connection:
        if storage_repository.measured_on(connection, today):
            return False
        values = _measure(connection, backup_dir)
        measured = storage_repository.insert_measurement(connection, values | {"measured_on": today})
    if measured:
        fields = {key: values[key] for key in ("database_bytes", "raw_text_bytes", "raw_text_count", "article_count", "backup_bytes")}
        logger.info("Storage measured", extra=fields)
    return measured


def history(engine: Engine) -> list[Measurement]:
    """The latest measurements, newest first."""
    with engine.connect() as connection:
        rows = storage_repository.measurements(connection, HISTORY_LIMIT)
    return [
        Measurement(row.measured_on, row.database_bytes, row.article_count, row.raw_text_count, row.raw_text_bytes,
                    row.backup_bytes, row.backup_free_bytes)
        for row in rows
    ]


def growth(measurements: list[Measurement], period: int) -> Growth | None:
    """The growth per day over the period, from measurements newest first; None before a second day is measured."""
    if len(measurements) < 2:
        return None
    latest = measurements[0]
    older = [each for each in measurements[1:] if (latest.measured_on - each.measured_on).days >= period]
    base = older[0] if older else measurements[-1]
    days = (latest.measured_on - base.measured_on).days
    return Growth(
        period, days, (latest.database_bytes - base.database_bytes) / days, (latest.raw_text_bytes - base.raw_text_bytes) / days,
        (latest.article_count - base.article_count) / days,
    )


def _measure(connection: Connection, backup_dir: Path) -> dict:
    raw_text_count, raw_text_bytes = storage_repository.raw_texts(connection)
    backups = backup_service.stored_backups(backup_dir, connection.engine.url.database)
    return {
        "database_bytes": storage_repository.database_bytes(connection),
        "table_bytes": storage_repository.table_bytes(connection),
        "article_count": storage_repository.article_count(connection),
        "raw_text_count": raw_text_count,
        "raw_text_bytes": raw_text_bytes,
        "backup_count": len(backups),
        "backup_bytes": sum(backup.size_bytes for backup in backups),
        "backup_free_bytes": backup_repository.free_bytes(backup_dir),
    }
