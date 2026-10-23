"""Backfill: a period of a Quelle fetched after the fact, always behind live work (userstories.md, Quellenadapter
und Ingestion, story 6).

A backfill is a chain of jobs, one request each: every job fetches the newest page of what is left of the period
and its effect queues the next one, until a page is not full (a feed returns all there is at once). Its jobs and
the full texts of its new articles are claimed only when no live job is due (job.priority), and their requests
take only free request slots (fetch_policy_service), so a backfill never makes current news wait.
"""
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Connection, Engine, Row

from app.db.models import BACKFILL_PRIORITY, BackfillStatus
from app.repositories import article_repository, backfill_repository, source_repository
from app.services import ingestion_service, job_service, platform_settings_service, source_service
from app.services.bulk import BulkResult
from app.services.fetch_policy_service import PoliteHttp
from app.sources import ADAPTERS
from app.sources.base import FetchedItem
from app.sources.errors import SourceRejected, SourceUnavailable
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    BackfillStatus.RUNNING: "Läuft",
    BackfillStatus.DONE: "Abgeschlossen",
    BackfillStatus.FAILED: "Fehlgeschlagen",
    BackfillStatus.CANCELLED: "Abgebrochen",
}
# A full page within one second: the next request steps this far back, the rest of that moment is not fetched.
STEP_BACK = timedelta(minutes=15)


class InvalidBackfill(ValueError):
    """Invalid entries from the backfill form. The message is shown to the admin as is."""


@dataclass(frozen=True)
class Backfill:
    id: int
    source_id: int
    period_start: datetime
    period_end: datetime
    cursor: datetime
    status: str
    articles_found: int
    articles_new: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None
    full_texts_stored: int
    full_texts_pending: int

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def is_running(self) -> bool:
        return self.status == BackfillStatus.RUNNING

    @property
    def first_day(self) -> date:
        return self.period_start.astimezone(UTC).date()

    @property
    def last_day(self) -> date:
        # The period ends before its end: midnight after the last day, or the moment it was started.
        return (self.period_end.astimezone(UTC) - timedelta(microseconds=1)).date()


def parse_period(form: Mapping[str, str], reach: timedelta | None, now: datetime) -> tuple[datetime, datetime]:
    """The period from the form: whole days in UTC, from the start of Von to the end of Bis, at most until now.

    `reach`: how far back the source's adapter can go (GDELT: 3 months); None for no limit of its own.
    """
    first = _day(form.get("period_start", ""), "Von")
    last = _day(form.get("period_end", ""), "Bis")
    today = now.astimezone(UTC).date()
    if last > today:
        raise InvalidBackfill("Bis: höchstens heute.")
    if first > last:
        raise InvalidBackfill("Von: darf nicht nach Bis liegen.")
    earliest = _earliest(reach, now)
    if earliest and first < earliest:
        raise InvalidBackfill(f"Von: frühestens {earliest:%d.%m.%Y}, weiter reicht diese Quelle nicht zurück.")
    start = datetime.combine(first, time(), UTC)
    end = min(datetime.combine(last + timedelta(days=1), time(), UTC), now)
    return start, end


def earliest_day(source: source_service.Source, now: datetime) -> date | None:
    """The earliest Von the form accepts for this source, None when its adapter sets no limit."""
    adapter = ADAPTERS.get(source.adapter_type)
    return _earliest(adapter.backfill_reach, now) if adapter else None


def start(engine: Engine, source_id: int, form: Mapping[str, str]) -> Backfill:
    """Start a backfill of an active source; its first job waits behind every live job."""
    source = source_service.get(engine, source_id)
    if source is None:
        raise InvalidBackfill("Quelle nicht gefunden.")
    if not source.is_active:
        raise InvalidBackfill("Die Quelle ist inaktiv; nur aktive Quellen werden abgerufen.")
    adapter = ADAPTERS.get(source.adapter_type)
    if adapter is None:
        raise InvalidBackfill(f"Kein Adapter für den Typ „{source.adapter_type}“.")
    period_start, period_end = parse_period(form, adapter.backfill_reach, datetime.now(UTC))
    with engine.begin() as connection:
        backfill_id = backfill_repository.insert_backfill(connection, source_id, period_start, period_end)
        _enqueue_page(connection, backfill_id)
    logger.info("Backfill started", extra={"backfill_id": backfill_id, "source_id": source_id})
    return next(backfill for backfill in list_for_source(engine, source_id) if backfill.id == backfill_id)


def list_for_source(engine: Engine, source_id: int) -> list[Backfill]:
    with engine.connect() as connection:
        return [_backfill(row) for row in backfill_repository.for_source(connection, source_id)]


def cancel(engine: Engine, source_id: int, backfill_ids: Iterable[int]) -> BulkResult:
    """Stop running backfills of this source, all or nothing; their articles and queued full texts stay.

    Atomic (requirements.md, "Destructive and Bulk Actions"): one that is not running blocks all of them.
    """
    backfill_ids = sorted(set(backfill_ids))
    if not backfill_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id: row for row in backfill_repository.lock_backfills(connection, backfill_ids)}
        blocked = []
        for backfill_id in backfill_ids:
            row = found.get(backfill_id)
            if row is None or row.source_id != source_id:
                blocked.append(f"Backfill #{backfill_id} (nicht gefunden)")
            elif row.status != BackfillStatus.RUNNING:
                blocked.append(f"Backfill #{backfill_id} ({STATUS_LABELS[row.status]})")
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        backfill_repository.finish(connection, backfill_ids, BackfillStatus.CANCELLED, None)
    return BulkResult(processed=len(backfill_ids), blocked=[])


def fetch_page(
    engine: Engine, backfill_id: int, contact: str, final_attempt: bool = False
) -> Callable[[Connection], None] | None:
    """First phase of a backfill job, outside any transaction: fetch the newest page of what is left.

    Returns the effect that stores the page's new articles - with their full-text jobs behind live work - and
    queues the next page, or ends the backfill: rejected by the source, still unreachable on the final attempt,
    or its source deactivated. None when the backfill is no longer running or its source is gone. Raises
    SourceUnavailable to be retried, HostBusy to wait for a free request slot.
    """
    with engine.connect() as connection:
        backfill = backfill_repository.get(connection, backfill_id)
    if backfill is None or backfill.status != BackfillStatus.RUNNING:
        return None
    source = source_service.get(engine, backfill.source_id)
    if source is None:
        return None
    if not source.is_active:
        return _end(backfill_id, BackfillStatus.CANCELLED, "Quelle inaktiv")
    adapter = ADAPTERS.get(source.adapter_type)
    if adapter is None:
        return _end(backfill_id, BackfillStatus.FAILED, f"Kein Adapter für den Typ „{source.adapter_type}“")
    settings = platform_settings_service.current(engine)
    min_interval = ingestion_service.request_interval(
        settings.domain_min_interval_seconds, source.min_request_interval_seconds
    )
    try:
        with HttpClient(contact) as http:
            polite = PoliteHttp(engine, http, min_interval, backfill=True)
            items = adapter.fetch_period(source, polite, backfill.period_start, backfill.cursor)
    except SourceRejected as error:
        return _end(backfill_id, BackfillStatus.FAILED, str(error))
    except SourceUnavailable as error:
        if final_attempt:
            return _end(backfill_id, BackfillStatus.FAILED, str(error))
        raise
    rows = ingestion_service.article_rows(source, items, backfill.period_start)
    rows = [row | {"backfill_id": backfill_id} for row in rows]
    cursor, skipped = next_cursor(adapter.page_size, items, backfill.cursor, backfill.period_start)

    def store(connection: Connection) -> None:
        # Cancelled meanwhile: nothing more of it. Deleted with its source: the lock finds nothing either.
        if not backfill_repository.lock_running(connection, backfill_id) or not source_repository.lock_sources(
            connection, [backfill.source_id]
        ):
            return
        article_ids = article_repository.insert_new(connection, rows)
        ingestion_service.enqueue_full_texts(connection, article_ids, BACKFILL_PRIORITY)
        backfill_repository.record_page(connection, backfill_id, len(rows), len(article_ids), cursor)
        if cursor is not None:
            _enqueue_page(connection, backfill_id)
        # IDs and counts only (datamodel.md, rule 13).
        page = {"backfill_id": backfill_id, "items": len(items), "new_articles": len(article_ids), "done": cursor is None}
        logger.info("Backfill page fetched", extra=page)
        if skipped:
            logger.warning("Backfill page full within one moment, rest of it skipped", extra={"backfill_id": backfill_id})

    return store


def next_cursor(
    page_size: int | None, items: Sequence[FetchedItem], cursor: datetime, period_start: datetime
) -> tuple[datetime | None, bool]:
    """Where the next request of a backfill ends - None when the period is done - and whether part of a moment
    is skipped because one page did not hold it."""
    if page_size is None or len(items) < page_size:
        return None, False
    dated = [item.published_at for item in items if item.published_at is not None]
    # One second past the oldest: what the source dated to that very second comes again and is stored once.
    following = min(dated) + timedelta(seconds=1) if dated else cursor
    skipped = following >= cursor
    if skipped:
        following = cursor - STEP_BACK
    return (following if following > period_start else None), skipped


def _enqueue_page(connection: Connection, backfill_id: int) -> None:
    # No dedupe key: the follow-up is queued by the effect of the job before it, which runs exactly once.
    job_service.enqueue(
        connection, job_service.BACKFILL_JOB_KIND, {"backfill_id": backfill_id}, priority=BACKFILL_PRIORITY
    )


def _end(backfill_id: int, status: BackfillStatus, error: str | None) -> Callable[[Connection], None]:
    return lambda connection: backfill_repository.finish(connection, [backfill_id], status, error)


def _earliest(reach: timedelta | None, now: datetime) -> date | None:
    # The first whole day within reach.
    return (now - reach).astimezone(UTC).date() + timedelta(days=1) if reach else None


def _day(raw: str, label: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise InvalidBackfill(f"{label}: bitte ein Datum wählen.") from None


def _backfill(row: Row) -> Backfill:
    return Backfill(**{field.name: row._mapping[field.name] for field in fields(Backfill)})
