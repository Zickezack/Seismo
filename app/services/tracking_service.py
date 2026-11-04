"""Tracked Wertpapiere (userstories.md, Live-Kurse und Kursdatenspeicher, story 2): their prices are collected
continuously at the highest resolution their source gives free, kept at full resolution for a while and then
compressed to hours - never deleted without a replacement, a time not collected kept as a gap, never filled
(requirements.md, Live-Kurse und Kursdatenspeicher, criteria 2 and 3; datamodel.md, rule 14).

What each source gives (the admin's decision of 2026-11-04): Alpaca the minutes of all US exchanges (SIP), free once
15 minutes old - asked for every 15 minutes from where the collection came, page by page, so a time the worker was
away is fetched afterwards and leaves no gap; SIX and EODHD only their latest price, asked for every minute - a time
nobody asked, or the source failed, during trading hours, is a gap (Erfassungslücke).

Switched on, an Alpaca one's minutes of the days before (Plattform-Einstellung) are fetched afterwards, a week per
step of a chain behind live work (backfill_minutes) - those switched on together in one request. The worker looks once
a minute (enqueue_due) and compresses once an hour (compress_due). A tracked Wertpapier of SIX
or EODHD needs no fetch of its Live-Kurs: its collection is its latest price (instrument_repository.live_due).
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine

from app.db.models import BACKFILL_PRIORITY, LIVE_PRIORITY, GapCause, InstrumentStatus, PriceResolution, PriceSource
from app.prices import alpaca
from app.repositories import instrument_repository, job_repository, price_repository
from app.services import job_service, platform_settings_service, price_service
from app.services.bulk import BulkResult
from app.services.fetch_policy_service import PoliteHttp
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

# The sources asked for their latest price, and how often a tracked one's is.
SNAPSHOT_SOURCES = (PriceSource.SIX, PriceSource.EODHD)
COLLECT_EVERY = timedelta(minutes=1)
# Alpaca's minutes: asked for this often, up to alpaca.SIP_DELAY ago.
MINUTES_EVERY = timedelta(minutes=15)
# A time without a collection during trading hours longer than this many intervals is a gap: a job waiting behind
# others, or a slow answer, is none.
GAP_FACTOR = 3
# A job's first phase may wait for a request slot; a price due this much early is asked for in the same minute.
EARLY = timedelta(seconds=30)
# SIX's trading hours (continuous trading and the closing auction), on weekdays; a holiday is asked for and answered,
# so it is no gap. EODHD's exchanges are many: every hour counts.
ZURICH = ZoneInfo("Europe/Zurich")
SIX_OPENS, SIX_CLOSES = time(9, 0), time(17, 30)
# The resolution a source's points are collected and compressed in.
COLLECTED = {PriceSource.ALPACA: PriceResolution.MINUTE, PriceSource.SIX: PriceResolution.QUOTE, PriceSource.EODHD: PriceResolution.QUOTE}
# A step of the backfill of minutes: a restart after a failure loses at most this much of its pages.
BACKFILL_STEP = timedelta(days=7)
# Transactions of one compression sweep, a day of a Wertpapier each: the first sweep after a long time catches up
# over several hours instead of one long run.
COMPRESS_STEPS = 200
INSERT_ROWS = 1000


# Gaps.

def trading_time(source: str, start: datetime, end: datetime) -> timedelta:
    """How much of the time from `start` to `end` lies within the source's trading hours."""
    if end <= start:
        return timedelta()
    if source != PriceSource.SIX:
        return end - start
    total, day = timedelta(), start.astimezone(ZURICH).date()
    while day <= end.astimezone(ZURICH).date():
        if day.weekday() < 5:
            opens = datetime.combine(day, SIX_OPENS, ZURICH)
            closes = datetime.combine(day, SIX_CLOSES, ZURICH)
            overlap = min(end, closes) - max(start, opens)
            if overlap > timedelta():
                total += overlap
        day += timedelta(days=1)
    return total


def is_gap(source: str, last: datetime | None, now: datetime, interval: timedelta = COLLECT_EVERY) -> bool:
    """Whether nothing was collected from `last` to `now` for longer than GAP_FACTOR intervals of trading time."""
    return last is not None and trading_time(source, last, now) > GAP_FACTOR * interval


def cause(last: datetime, last_error_at: datetime | None) -> GapCause:
    """A gap the source caused - it failed, or sent no price for it, since the last collection - or one nobody asked
    about: the worker stopped, the queue stuck."""
    return GapCause.SOURCE_ERROR if last_error_at is not None and last_error_at > last else GapCause.NOT_ASKED


# Tracking on and off.

def set_tracked(engine: Engine, instrument_ids, tracked: bool) -> BulkResult:
    """Switch tracking on or off, all or nothing (requirements.md, "Destructive and Bulk Actions"). On: not for one no
    longer traded, and never beyond the most tracked of the Plattform-Einstellungen."""
    instrument_ids = sorted(set(instrument_ids))
    if not instrument_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id: row for row in instrument_repository.lock_instruments(connection, instrument_ids)}
        blocked = [f"Wertpapier #{each} (nicht gefunden)" for each in instrument_ids if each not in found]
        if tracked:
            blocked += [f"{row.name} (nicht mehr gehandelt)" for row in found.values() if row.status != InstrumentStatus.ACTIVE]
            settings = platform_settings_service.load(connection)
            most = settings.tracked_instruments_max
            # Counted under the rows' locks of this batch; two batches at once may each see the other's not yet.
            total = instrument_repository.count_tracked(connection, excluding=instrument_ids) + len(instrument_ids)
            if total > most:
                blocked.append(f"höchstens {most} verfolgte Wertpapiere (Plattform-Einstellungen), es wären {total}")
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        backfill_days = settings.minute_backfill_days if tracked else 0
        instrument_repository.set_tracked(connection, instrument_ids, tracked, backfill_days)
    logger.info("Tracking changed", extra={"instruments": len(instrument_ids), "tracked": tracked})
    return BulkResult(processed=len(instrument_ids), blocked=[])


# Collection.

def enqueue_due(engine: Engine, now: datetime | None = None) -> list[int]:
    """Queue, once a minute: the latest price of the tracked Wertpapiere of SIX and EODHD not collected within the
    minute, a job per batch; and Alpaca's minutes once 15 more are free, one chain at a time. None from a source whose
    key is missing."""
    now = now or datetime.now(UTC)
    job_ids = []
    with engine.begin() as connection:
        for source in SNAPSHOT_SOURCES:
            if not price_service.configured(source):
                continue
            module = price_service.SOURCES[source]
            due = [row.id for row in instrument_repository.tracked_of(connection, source) if _asked(row) < now - COLLECT_EVERY + EARLY]
            for start in range(0, len(due), module.BATCH):
                batch = due[start : start + module.BATCH]
                job_ids.append(job_service.enqueue(
                    connection, job_service.COLLECT_PRICES_JOB_KIND, {"source": source, "instrument_ids": batch},
                    dedupe_key=f"{job_service.COLLECT_PRICES_JOB_KIND}:{source}:{batch[0]}",
                    max_attempts=job_service.FETCH_PRICES_MAX_ATTEMPTS, priority=LIVE_PRIORITY,
                ))
        if price_service.configured(PriceSource.ALPACA):
            tracked = instrument_repository.tracked_of(connection, PriceSource.ALPACA)
            if tracked and _through(tracked[0], now) < minutes_end(now) - MINUTES_EVERY + EARLY:
                if not job_repository.any_active(connection, [job_service.FETCH_MINUTES_JOB_KIND]):
                    job_ids.append(job_service.enqueue(connection, job_service.FETCH_MINUTES_JOB_KIND, {}, priority=LIVE_PRIORITY))
            backfill = instrument_repository.backfill_due(connection)
            if backfill and backfill[0].backfill_through < minutes_end(now) and not job_repository.any_active(
                connection, [job_service.BACKFILL_MINUTES_JOB_KIND]
            ):
                job_ids.append(job_service.enqueue(connection, job_service.BACKFILL_MINUTES_JOB_KIND, {}, priority=BACKFILL_PRIORITY))
    return [job_id for job_id in job_ids if job_id is not None]


def _asked(row) -> datetime:
    """When it was asked for last, successfully or not."""
    return max(moment for moment in (row.collected_through, row.last_error_at, datetime.min.replace(tzinfo=UTC)) if moment)


def _through(row, now: datetime) -> datetime:
    """How far its collection came; one never collected - tracked before collection began - from a day ago."""
    return row.collected_through or now - timedelta(days=1)


def minutes_end(now: datetime) -> datetime:
    """Up to where Alpaca's minutes are free: SIP_DELAY ago, a whole minute."""
    return (now - alpaca.SIP_DELAY).replace(second=0, microsecond=0)


def collect(
    engine: Engine, source: str, instrument_ids: list[int], contact: str, reserved_slot: datetime | None = None
) -> Callable[[Connection], None] | None:
    """First phase of a job "Kurse erfassen": the latest prices of a batch of tracked Wertpapiere of SIX or EODHD.
    The effect stores them, each once, and records each one collected up to now - after a gap, the gap. None when none
    of them is tracked any more."""
    with engine.connect() as connection:
        rows = [row for row in instrument_repository.by_ids(connection, instrument_ids) if row.tracked and row.source == source]
    if not rows:
        return None
    prices, missing, by_ref = price_service.ask(engine, source, rows, contact, reserved_slot)
    now = datetime.now(UTC)
    collected = sorted({price["instrument_id"] for price in prices})

    def store(connection: Connection) -> None:
        stored = price_repository.insert_prices(connection, prices)
        state = instrument_repository.collection_state(connection, collected)
        gaps = 0
        for instrument_id in collected:
            last = state[instrument_id].collected_through
            if is_gap(source, last, now):
                price_repository.insert_gap(connection, instrument_id, last, now, cause(last, state[instrument_id].last_error_at))
                gaps += 1
        instrument_repository.record_collected(connection, collected, now)
        instrument_repository.record_fetched(connection, collected)
        for each in missing:
            instrument_repository.record_error(connection, [by_ref[each.ref]], each.reason)
        # IDs and counts only (datamodel.md, rule 13).
        logger.info("Prices collected", extra={"source": source, "instruments": len(by_ref), "new": stored, "missing": len(missing), "gaps": gaps})

    return store


def fetch_minutes(
    engine: Engine, payload: dict, contact: str, reserved_slot: datetime | None = None
) -> Callable[[Connection], None] | None:
    """First phase of a job "Minutenkurse abrufen": one page of Alpaca's minutes of a batch of tracked Wertpapiere -
    the batch the job names, else those collected least far, from the earliest of them up to minutes_end. The effect
    stores them and queues the next page, or records the batch collected that far. None when nothing is due."""
    if payload.get("instruments"):
        instruments, start, end = payload["instruments"], datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"])
    else:
        with engine.connect() as connection:
            due = instrument_repository.tracked_of(connection, PriceSource.ALPACA)
        now = datetime.now(UTC)
        end = minutes_end(now)
        due = [row for row in due if _through(row, now) < end][: alpaca.BAR_SYMBOLS]
        if not due:
            return None
        instruments = {}
        for row in due:
            instruments.setdefault(row.symbol, row.id)
        start = min(_through(row, now) for row in due)
    with engine.connect() as connection:
        min_interval = platform_settings_service.load(connection).domain_min_interval_seconds
    with HttpClient(contact) as http:
        polite = PoliteHttp(engine, http, min_interval, reserved_slot)
        bars, token = alpaca.minute_bars(polite, list(instruments), start, end, payload.get("page_token"))
    prices = _minutes(bars, instruments)
    page = {"instruments": instruments, "start": start.isoformat(), "end": end.isoformat()}

    def store(connection: Connection) -> None:
        stored = sum(
            price_repository.insert_prices(connection, prices[begin : begin + INSERT_ROWS]) for begin in range(0, len(prices), INSERT_ROWS)
        )
        if token:
            job_service.enqueue(connection, job_service.FETCH_MINUTES_JOB_KIND, page | {"page_token": token}, priority=LIVE_PRIORITY)
        else:
            instrument_repository.record_collected(connection, list(instruments.values()), end)
        logger.info("Minutes fetched", extra={"instruments": len(instruments), "prices": len(prices), "new": stored, "done": not token})

    return store


def backfill_minutes(engine: Engine, payload: dict, contact: str) -> Callable[[Connection], None] | None:
    """First phase of a job "Minutenhistorie nachholen": one page of Alpaca's minutes from before tracking began - of
    the batch the job names, else of those the backfill came least far with that share where it stands and where it
    ends (switched on together), a BACKFILL_STEP at most. A backfill request: only while the host is free, never
    reserving a slot. The effect stores them and queues the next page, or records the step and queues the next. None
    when nothing is due."""
    if payload.get("instruments"):
        instruments, start, end = payload["instruments"], datetime.fromisoformat(payload["start"]), datetime.fromisoformat(payload["end"])
    else:
        with engine.connect() as connection:
            due = instrument_repository.backfill_due(connection)
        if not due:
            return None
        first = due[0]
        # The last minutes before tracking began are free only once 15 minutes old, like any.
        start, end = first.backfill_through, min(first.backfill_through + BACKFILL_STEP, first.tracked_since, minutes_end(datetime.now(UTC)))
        if end <= start:
            return None
        batch = [row for row in due if (row.backfill_through, row.tracked_since) == (first.backfill_through, first.tracked_since)]
        instruments = {}
        for row in batch[: alpaca.BAR_SYMBOLS]:
            instruments.setdefault(row.symbol, row.id)
    with engine.connect() as connection:
        min_interval = platform_settings_service.load(connection).domain_min_interval_seconds
    with HttpClient(contact) as http:
        polite = PoliteHttp(engine, http, min_interval, backfill=True)
        bars, token = alpaca.minute_bars(polite, list(instruments), start, end, payload.get("page_token"))
    prices = _minutes(bars, instruments)
    page = {"instruments": instruments, "start": start.isoformat(), "end": end.isoformat()}

    def store(connection: Connection) -> None:
        stored = sum(
            price_repository.insert_prices(connection, prices[begin : begin + INSERT_ROWS]) for begin in range(0, len(prices), INSERT_ROWS)
        )
        kind = job_service.BACKFILL_MINUTES_JOB_KIND
        if token:
            job_service.enqueue(connection, kind, page | {"page_token": token}, priority=BACKFILL_PRIORITY)
        else:
            instrument_repository.record_backfilled(connection, list(instruments.values()), end)
            # No dedupe key: a follow-up is queued by the effect of the job before it, which runs exactly once.
            job_service.enqueue(connection, kind, {}, priority=BACKFILL_PRIORITY)
        logger.info("Minutes backfilled", extra={"instruments": len(instruments), "prices": len(prices), "new": stored, "done": not token})

    return store


def _minutes(bars, instruments: dict[str, int]) -> list[dict]:
    """Alpaca's minutes as Kurspunkte: of all exchanges (SIP), free only once 15 minutes old - delayed."""
    return [
        {
            "instrument_id": instruments[bar.symbol], "resolution": PriceResolution.MINUTE, "at": bar.at, "open": bar.open,
            "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume, "previous_close": None,
            "source": PriceSource.ALPACA, "delayed": True,
        }
        for bar in bars if bar.symbol in instruments
    ]


# What tracking takes (Live-Kurse und Kursdatenspeicher, criterion 5): the measure a longer full resolution is decided on.

NEED_PERIOD = timedelta(days=7)
# Measured over less, the day's trading hours would decide the number (seen on the dev data, 2026-11-04: minutes after
# the first was tracked, none of its points lay within the time measured).
NEED_MIN_DAYS = 1


@dataclass(frozen=True)
class Need:
    tracked: int  # Wertpapiere tracked now
    points_per_day: float  # full-resolution points of one of them per day, as measured
    measured_days: float  # over this many days, at most NEED_PERIOD
    full_resolution_days: int  # 0: unlimited

    @property
    def measured(self) -> bool:
        """Measured over a day at least: before, a number per day would be a guess."""
        return self.measured_days >= NEED_MIN_DAYS

    def bytes_per_day(self, bytes_per_point: float) -> float:
        """Of all tracked ones together."""
        return self.points_per_day * self.tracked * bytes_per_point


def need(connection: Connection, now: datetime | None = None) -> Need:
    """The full-resolution points the tracked Wertpapiere collected per Wertpapier and day, over the last NEED_PERIOD
    or since the first of them was tracked."""
    now = now or datetime.now(UTC)
    days = platform_settings_service.load(connection).price_full_resolution_days
    rows = instrument_repository.tracked_since_of(connection)
    if not rows:
        return Need(0, 0.0, 0.0, days)
    since = max(now - NEED_PERIOD, min(row.tracked_since for row in rows))
    measured = (now - since) / timedelta(days=1)
    points = price_repository.recent_count(connection, [row.id for row in rows], set(COLLECTED.values()), since)
    return Need(len(rows), points / len(rows) / measured if measured > 0 else 0.0, measured, days)


# Compression.

@dataclass(frozen=True)
class CompressResult:
    points: int = 0  # replaced
    hours: int = 0  # made of them
    steps: int = 0  # transactions


def compress_due(engine: Engine, now: datetime | None = None, steps: int = COMPRESS_STEPS) -> CompressResult:
    """Compress the full-resolution points older than the period of the Plattform-Einstellungen to hours, a day of
    a Wertpapier per transaction, at most `steps`; nothing while it is 0 (unlimited) or another worker compresses.
    The worker calls it once an hour."""
    now = now or datetime.now(UTC)
    with engine.connect() as connection:
        days = platform_settings_service.load(connection).price_full_resolution_days
        candidates = instrument_repository.compressible(connection)
    if not days:
        return CompressResult()
    # Whole hours only: an hour still being filled is not compressed.
    before = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    points = hours = done = 0
    for candidate in candidates:
        resolution, after = COLLECTED[candidate.source], None
        while done < steps:
            with engine.begin() as connection:
                if not price_repository.try_lock_compression(connection):
                    return CompressResult(points, hours, done)
                oldest = price_repository.oldest(connection, candidate.id, resolution, after, before)
                if oldest is None:
                    break
                start = oldest.replace(minute=0, second=0, microsecond=0)
                after = min(start + timedelta(days=1), before)
                replaced, made = price_repository.compress(connection, candidate.id, resolution, start, after)
            points, hours, done = points + replaced, hours + made, done + 1
    if points:
        logger.info("Prices compressed", extra={"points": points, "hours": hours, "steps": done})
    return CompressResult(points, hours, done)
