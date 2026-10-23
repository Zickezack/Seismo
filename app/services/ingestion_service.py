"""Live ingestion: due Quellen are read by their adapters into Artikel, and each new article's full text into
the raw-data layer (userstories.md, Quellenadapter und Ingestion).

The worker queues a fetch job for every due source once a minute (app/worker/main.py). The job reads the
source outside any transaction and stores the new articles - with a full-text job for each - in the
transaction that marks it done, so a retried job never stores an article twice (app/jobs/runner.py). Every
request goes through the fetch policy: robots.txt and a minimum interval per host (fetch_policy_service).

Every failed fetch of a source is kept at the source - last error, failures in a row; a success resets the
count - and logged (story 4). A full-text job keeps trying for about a day before the full text counts as
failed, so a host that is down for hours loses nothing.
"""
import hashlib
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine

from app.db.models import LIVE_PRIORITY, FullTextStatus
from app.repositories import article_repository, raw_text_repository, source_repository
from app.services import job_service, platform_settings_service, source_service
from app.services.fetch_policy_service import PoliteHttp
from app.services.bulk import BulkResult
from app.services.source_service import Source
from app.sources import ADAPTERS
from app.sources.base import FetchedItem
from app.sources.errors import FetchDisallowed, HostBusy, SourceError, SourceRejected, SourceUnavailable
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

# The unique index on (source_id, item_key) holds keys of limited size; longer ones are stored as their hash.
MAX_KEY_BYTES = 1000
MAX_URL_LENGTH = 2000
MAX_TITLE_LENGTH = 1000
# What the raw-data layer keeps: what the normalisation can turn into text.
FULL_TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "application/pdf"}
FULL_TEXT_ACCEPT = "text/html, application/xhtml+xml;q=0.9, application/pdf;q=0.8, text/plain;q=0.7, */*;q=0.1"


def enqueue_due_fetches(engine: Engine) -> list[int]:
    """Queue a fetch job for every active source whose next fetch is due; the ids of the new jobs."""
    with engine.begin() as connection:
        return _enqueue(connection, source_repository.claim_due(connection))


def fetch_now(engine: Engine, source_ids: Iterable[int]) -> BulkResult:
    """Queue a fetch of the selected sources at once, all or nothing; an inactive source blocks it."""
    source_ids = sorted(set(source_ids))
    if not source_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id: row for row in source_repository.lock_sources(connection, source_ids)}
        blocked = []
        for source_id in source_ids:
            row = found.get(source_id)
            if row is None:
                blocked.append(f"Quelle #{source_id} (nicht gefunden)")
            elif not row.is_active:
                blocked.append(f"{row.name} (inaktiv)")
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        source_repository.schedule_next_fetch(connection, source_ids)
        # A source whose fetch already waits or runs keeps that job (dedupe key).
        _enqueue(connection, source_ids)
    return BulkResult(processed=len(source_ids), blocked=[])


def fetch_source(
    engine: Engine, source_id: int, contact: str, reserved_slot: datetime | None = None
) -> Callable[[Connection], None] | None:
    """First phase of a fetch job, outside any transaction: read the source through its adapter.

    Returns the effect that stores the new articles, or None when the source is gone or inactive by now.
    Raises SourceRejected (a retry would not help), SourceUnavailable or HostBusy (app/sources/errors.py);
    every error but HostBusy is first recorded at the source.
    """
    source = source_service.get(engine, source_id)
    if source is None or not source.is_active:
        return None
    settings = platform_settings_service.current(engine)
    min_interval = request_interval(settings.domain_min_interval_seconds, source.min_request_interval_seconds)
    try:
        adapter = ADAPTERS.get(source.adapter_type)
        if adapter is None:
            raise SourceRejected(f"Kein Adapter für den Typ „{source.adapter_type}“")
        with HttpClient(contact) as http:
            items = adapter.fetch(source, PoliteHttp(engine, http, min_interval, reserved_slot))
    except HostBusy:
        raise
    except Exception as error:
        _record_failure(engine, source_id, error)
        raise
    rows = article_rows(source, items, datetime.now(UTC) - timedelta(days=settings.article_max_age_days))

    def store(connection: Connection) -> None:
        # Deleted meanwhile: its articles would point to nothing.
        if not source_repository.lock_sources(connection, [source_id]):
            return
        article_ids = article_repository.insert_new(connection, rows)
        enqueue_full_texts(connection, article_ids, LIVE_PRIORITY)
        source_repository.mark_fetched(connection, source_id)
        # IDs and counts only, never titles (datamodel.md, rule 13).
        logger.info("Source fetched", extra={"source_id": source_id, "items": len(items), "new_articles": len(article_ids)})

    return store


def enqueue_full_texts(connection: Connection, article_ids: Iterable[int], priority: int) -> None:
    """A full-text job for each new article, in the transaction that stores them; a backfill's behind live work."""
    kind = job_service.FETCH_ARTICLE_JOB_KIND
    for article_id in article_ids:
        job_service.enqueue(
            connection,
            kind,
            {"article_id": article_id},
            dedupe_key=f"{kind}:{article_id}",
            max_attempts=job_service.FETCH_ARTICLE_MAX_ATTEMPTS,
            priority=priority,
        )


def _record_failure(engine: Engine, source_id: int, error: Exception) -> None:
    ours = isinstance(error, SourceError)
    message = str(error)[: job_service.MAX_ERROR_LENGTH] if ours else job_service.describe_error(error)
    with engine.begin() as connection:
        failures = source_repository.record_failure(connection, source_id, message)
    fields = {"source_id": source_id, "error_type": type(error).__name__, "consecutive_failures": failures}
    # Only an adapter's own words (HTTP status, timeout, robots.txt); its detail and any other message may
    # quote what the source sent (datamodel.md, rule 13).
    if ours:
        fields["error"] = error.summary
    logger.warning("Source fetch failed", extra=fields)


def request_interval(platform_seconds: int, source_seconds: int | None) -> int:
    """The minimum interval for a request made for a source: its own one only ever raises the platform's."""
    return max(platform_seconds, source_seconds or 0)


def fetch_full_text(
    engine: Engine,
    article_id: int,
    contact: str,
    reserved_slot: datetime | None = None,
    final_attempt: bool = False,
    backfill: bool = False,
) -> Callable[[Connection], None] | None:
    """First phase of a full-text job: fetch the article's page. Returns the effect that records the outcome.

    A lasting outcome (stored, disallowed by robots.txt, a content type not kept, HTTP 4xx, source inactive
    or deleted) settles the article. A passing failure (timeout, HTTP 5xx) raises SourceUnavailable to be
    retried; on the final attempt it settles the article as failed instead. HostBusy defers the job. A
    backfill's full text takes only a free request slot (fetch_policy_service).
    """
    with engine.connect() as connection:
        article = article_repository.get_for_full_text(connection, article_id)
    if article is None or article.full_text_status != FullTextStatus.PENDING:
        return None
    if not article.source_active:
        reason = "Quelle gelöscht" if article.source_active is None else "Quelle inaktiv"
        return _settle(article_id, FullTextStatus.SKIPPED, reason)
    platform_interval = platform_settings_service.current(engine).domain_min_interval_seconds
    min_interval = request_interval(platform_interval, article.source_min_request_interval_seconds)
    try:
        with HttpClient(contact) as http:
            polite = PoliteHttp(engine, http, min_interval, reserved_slot, backfill=backfill)
            response = polite.get(article.url, FULL_TEXT_ACCEPT)
    except FetchDisallowed as error:
        return _settle(article_id, FullTextStatus.DISALLOWED, str(error))
    except SourceRejected as error:
        return _settle(article_id, FullTextStatus.FAILED, str(error))
    except SourceUnavailable as error:
        if final_attempt:
            return _settle(article_id, FullTextStatus.FAILED, str(error))
        raise
    media_type = (response.content_type or "").split(";")[0].strip().lower()
    if media_type not in FULL_TEXT_TYPES:
        return _settle(article_id, FullTextStatus.UNSUPPORTED, f"Inhaltstyp {media_type or 'unbekannt'} wird nicht gespeichert")

    def store(connection: Connection) -> None:
        if article_repository.finish_full_text(connection, article_id, FullTextStatus.STORED, None):
            raw_text_repository.insert_raw_text(connection, article_id, response.content, response.content_type, response.url)

    return store


def _settle(article_id: int, status: FullTextStatus, error: str) -> Callable[[Connection], None]:
    return lambda connection: article_repository.finish_full_text(connection, article_id, status, error)


def article_rows(source: Source, items: Iterable[FetchedItem], cutoff: datetime) -> list[dict]:
    """The items as article rows, each key once.

    Items published before `cutoff` are left to the backfill (Plattform-Einstellung "Höchstalter neuer
    Artikel"); an item without a date always comes, since its age is unknown. Items without a usable web
    link are dropped. Source name and URL are copied, so an article keeps them after its source is deleted.
    """
    rows = {}
    for item in items:
        if item.published_at is not None and item.published_at < cutoff:
            continue
        url = item.url.strip()
        if len(url) > MAX_URL_LENGTH or urlsplit(url).scheme not in ("http", "https"):
            continue
        key = _item_key(item.key)
        rows.setdefault(
            key,
            {
                "source_id": source.id,
                "source_name": source.name,
                "source_url": source.url,
                "item_key": key,
                "url": url,
                "title": item.title[:MAX_TITLE_LENGTH] if item.title else None,
                "published_at": item.published_at,
                "language": item.language or source.language,
            },
        )
    return list(rows.values())


def _item_key(key: str) -> str:
    if len(key.encode()) <= MAX_KEY_BYTES:
        return key
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def _enqueue(connection: Connection, source_ids: list[int]) -> list[int]:
    kind = job_service.FETCH_SOURCE_JOB_KIND
    job_ids = [
        job_service.enqueue(connection, kind, {"source_id": source_id}, dedupe_key=f"{kind}:{source_id}")
        for source_id in source_ids
    ]
    return [job_id for job_id in job_ids if job_id is not None]
