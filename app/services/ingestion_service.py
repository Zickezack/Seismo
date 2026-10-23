"""Live ingestion: due Quellen are read by their adapters into Artikel (userstories.md, Quellenadapter und Ingestion).

The worker queues a fetch job for every due source once a minute (app/worker/main.py). The job reads the
source outside any transaction and stores the new articles in the transaction that marks it done, so a
retried job never stores an article twice (app/jobs/runner.py).
"""
import hashlib
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine

from app.repositories import article_repository, source_repository
from app.services import job_service, platform_settings_service, source_service
from app.services.bulk import BulkResult
from app.services.source_service import Source
from app.sources import ADAPTERS
from app.sources.base import FetchedItem
from app.sources.errors import SourceRejected
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

# The unique index on (source_id, item_key) holds keys of limited size; longer ones are stored as their hash.
MAX_KEY_BYTES = 1000
MAX_URL_LENGTH = 2000
MAX_TITLE_LENGTH = 1000


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


def fetch_source(engine: Engine, source_id: int, contact: str) -> Callable[[Connection], None] | None:
    """First phase of a fetch job, outside any transaction: read the source through its adapter.

    Returns the effect that stores the new articles, or None when the source is gone or inactive by now.
    Raises SourceRejected (a retry would not help) or SourceUnavailable (app/sources/errors.py).
    """
    source = source_service.get(engine, source_id)
    if source is None or not source.is_active:
        return None
    adapter = ADAPTERS.get(source.adapter_type)
    if adapter is None:
        raise SourceRejected(f"Kein Adapter für den Typ „{source.adapter_type}“")
    with HttpClient(contact) as http:
        items = adapter.fetch(source, http)
    max_age_days = platform_settings_service.current(engine).article_max_age_days
    rows = article_rows(source, items, datetime.now(UTC) - timedelta(days=max_age_days))

    def store(connection: Connection) -> None:
        # Deleted meanwhile: its articles would point to nothing.
        if not source_repository.lock_sources(connection, [source_id]):
            return
        stored = article_repository.insert_new(connection, rows)
        source_repository.mark_fetched(connection, source_id)
        # IDs and counts only, never titles (datamodel.md, rule 13).
        logger.info("Source fetched", extra={"source_id": source_id, "items": len(items), "new_articles": stored})

    return store


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
