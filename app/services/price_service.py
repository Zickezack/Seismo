"""Live prices (userstories.md, Live-Kurse und Kursdatenspeicher, story 1): the worker fetches the latest price of
every active Wertpapier once a minute - a job per BATCH of them, one request each - and keeps each price as received,
unadjusted, with the time it became available (datamodel.md, rule 14). A price the source sends again with the same
time, the market closed, is kept once. Only while the source's key is set: without one nothing is queued, and the
Wertpapiere page says so.

The first source is EODHD, delayed 15 to 20 minutes (app/prices/eodhd.py); every price shown names it, its time, the
exchange, the currency and the delay (requirements.md, Live-Kurse und Kursdatenspeicher, criterion 1).
"""
import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Connection, Engine

from app.db.models import LIVE_PRIORITY, PriceResolution
from app.prices import eodhd
from app.repositories import instrument_repository, price_repository
from app.services import job_service, platform_settings_service
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import HostBusy, SourceError
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

SOURCE = eodhd


def enqueue_due(engine: Engine) -> list[int]:
    """Queue a fetch for the active Wertpapiere, one job per batch; none while the source's key is missing. The worker
    calls it once a minute."""
    if not SOURCE.configured():
        return []
    kind = job_service.FETCH_PRICES_JOB_KIND
    with engine.begin() as connection:
        ids = [row.id for row in instrument_repository.active(connection)]
        batches = [ids[start : start + SOURCE.BATCH] for start in range(0, len(ids), SOURCE.BATCH)]
        job_ids = [
            job_service.enqueue(
                connection, kind, {"instrument_ids": batch}, dedupe_key=f"{kind}:{batch[0]}",
                max_attempts=job_service.FETCH_PRICES_MAX_ATTEMPTS, priority=LIVE_PRIORITY,
            )
            for batch in batches
        ]
    return [job_id for job_id in job_ids if job_id is not None]


def fetch(
    engine: Engine, instrument_ids: list[int], contact: str, reserved_slot: datetime | None = None
) -> Callable[[Connection], None] | None:
    """First phase of a fetch job, outside any transaction: ask the source for the batch's latest prices. Returns the
    effect that stores them, or None when none of them is active any more. Every error but HostBusy is first recorded
    at the batch's Wertpapiere; SourceError and HostBusy as app/sources/errors.py says."""
    with engine.connect() as connection:
        rows = [row for row in instrument_repository.by_ids(connection, instrument_ids) if row.is_active]
        min_interval = platform_settings_service.load(connection).domain_min_interval_seconds
    if not rows:
        return None
    by_code = {(row.symbol, row.exchange): row.id for row in rows}
    try:
        with HttpClient(contact) as http:
            quotes, missing = SOURCE.latest(PoliteHttp(engine, http, min_interval, reserved_slot), list(by_code))
    except HostBusy:
        raise
    except Exception as error:
        message = error.summary if isinstance(error, SourceError) else type(error).__name__
        with engine.begin() as connection:
            instrument_repository.record_error(connection, list(by_code.values()), message)
        logger.warning("Price fetch failed", extra={"instruments": len(by_code), "error": message})
        raise
    prices = [
        {
            "instrument_id": by_code[(quote.symbol, quote.exchange)], "resolution": PriceResolution.QUOTE, "at": quote.at,
            "open": quote.open, "high": quote.high, "low": quote.low, "close": quote.close, "volume": quote.volume,
            "previous_close": quote.previous_close, "source": SOURCE.NAME, "delayed": SOURCE.DELAYED,
        }
        for quote in quotes
    ]

    def store(connection: Connection) -> None:
        stored = price_repository.insert_prices(connection, prices)
        instrument_repository.record_fetched(connection, [price["instrument_id"] for price in prices])
        for each in missing:
            instrument_repository.record_error(connection, [by_code[(each.symbol, each.exchange)]], each.reason)
        # IDs and counts only (datamodel.md, rule 13).
        logger.info("Prices fetched", extra={"instruments": len(by_code), "prices": len(prices), "new": stored, "missing": len(missing)})

    return store
