"""Live prices (userstories.md, Live-Kurse und Kursdatenspeicher, story 1; Historische Markt- und Ereignisdaten, story 1):
the worker fetches the latest price of every Wertpapier with a Live-Kurs at the interval of the Plattform-Einstellungen
- a job per batch of one source's, one request each - and keeps each price as received, unadjusted, with the time it
became available (datamodel.md, rule 14). A price the source sends again with the same time, the market closed, is
kept once.

Each Wertpapier is fetched from its own source: SIX for Swiss ones, delayed 15 minutes, without a key; Alpaca for US
ones, real time from IEX, while its keys are set; EODHD for others, delayed, while its key is set. Every price shown
names its source, its time, the exchange, the currency and the delay (requirements.md, Live-Kurse und
Kursdatenspeicher, criterion 1).
"""
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, Engine

from app.db.models import LIVE_PRIORITY, PriceResolution
from app.prices import alpaca, eodhd, six
from app.prices.base import Missing
from app.repositories import instrument_repository, price_repository
from app.services import job_service, platform_settings_service
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import HostBusy, SourceError
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

SOURCES = {alpaca.NAME: alpaca, six.NAME: six, eodhd.NAME: eodhd}
# A price is due this much before its interval is over: the worker looks once a minute.
EARLY = timedelta(seconds=30)


def configured(source: str) -> bool:
    """Whether the source can be asked: its keys are set, or it needs none."""
    module = SOURCES[source]
    return module.configured() if hasattr(module, "configured") else True


def enqueue_due(engine: Engine, now: datetime | None = None) -> list[int]:
    """Queue a fetch for the Wertpapiere with a Live-Kurs not asked for within the interval, one job per batch of one
    source's; none of a source whose key is missing. The worker calls it once a minute."""
    now = now or datetime.now(UTC)
    kind = job_service.FETCH_PRICES_JOB_KIND
    job_ids = []
    with engine.begin() as connection:
        interval = timedelta(minutes=platform_settings_service.load(connection).price_interval_minutes)
        for name, module in SOURCES.items():
            if not configured(name):
                continue
            ids = [row.id for row in instrument_repository.live_due(connection, name, now - interval + EARLY)]
            for start in range(0, len(ids), module.BATCH):
                batch = ids[start : start + module.BATCH]
                job_ids.append(job_service.enqueue(
                    connection, kind, {"source": name, "instrument_ids": batch}, dedupe_key=f"{kind}:{name}:{batch[0]}",
                    max_attempts=job_service.FETCH_PRICES_MAX_ATTEMPTS, priority=LIVE_PRIORITY,
                ))
    return [job_id for job_id in job_ids if job_id is not None]


def ask(
    engine: Engine, source: str, rows: list, contact: str, reserved_slot: datetime | None = None
) -> tuple[list[dict], list[Missing], dict[str, int]]:
    """Ask the source for the latest prices of the Wertpapiere `rows` (id, symbol, source_ref), outside any
    transaction: the prices as rows to store, those it sent none for, and each Wertpapier's id by what it was asked
    by. Every error but HostBusy is first recorded at them; SourceError and HostBusy as app/sources/errors.py says."""
    module = SOURCES[source]
    with engine.connect() as connection:
        min_interval = platform_settings_service.load(connection).domain_min_interval_seconds
    by_ref: dict[str, int] = {}
    for row in rows:
        by_ref.setdefault(row.symbol if getattr(module, "BY_SYMBOL", False) else row.source_ref, row.id)
    try:
        with HttpClient(contact) as http:
            quotes, missing = module.latest(PoliteHttp(engine, http, min_interval, reserved_slot), list(by_ref))
    except HostBusy:
        raise
    except Exception as error:
        message = error.summary if isinstance(error, SourceError) else type(error).__name__
        with engine.begin() as connection:
            instrument_repository.record_error(connection, list(by_ref.values()), message)
        logger.warning("Price fetch failed", extra={"source": source, "instruments": len(by_ref), "error": message})
        raise
    prices = [
        {
            "instrument_id": by_ref[quote.ref], "resolution": PriceResolution.QUOTE, "at": quote.at, "open": quote.open,
            "high": quote.high, "low": quote.low, "close": quote.close, "volume": quote.volume,
            "previous_close": quote.previous_close, "source": source, "delayed": module.DELAYED,
        }
        for quote in quotes if quote.ref in by_ref
    ]
    return prices, [each for each in missing if each.ref in by_ref], by_ref


def fetch(
    engine: Engine, source: str, instrument_ids: list[int], contact: str, reserved_slot: datetime | None = None
) -> Callable[[Connection], None] | None:
    """First phase of a fetch job, outside any transaction: ask the source for the batch's latest prices. Returns the
    effect that stores them, or None when none of them has a Live-Kurs any more. Every error but HostBusy is first
    recorded at the batch's Wertpapiere; SourceError and HostBusy as app/sources/errors.py says."""
    with engine.connect() as connection:
        rows = [row for row in instrument_repository.by_ids(connection, instrument_ids) if row.live and row.source == source]
    if not rows:
        return None
    prices, missing, by_ref = ask(engine, source, rows, contact, reserved_slot)

    def store(connection: Connection) -> None:
        stored = price_repository.insert_prices(connection, prices)
        instrument_repository.record_fetched(connection, [price["instrument_id"] for price in prices])
        for each in missing:
            instrument_repository.record_error(connection, [by_ref[each.ref]], each.reason)
        # IDs and counts only (datamodel.md, rule 13).
        logger.info(
            "Prices fetched",
            extra={"source": source, "instruments": len(by_ref), "prices": len(prices), "new": stored, "missing": len(missing)},
        )

    return store
