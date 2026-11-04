"""Historical market data (userstories.md, Historische Markt- und Ereignisdaten, story 1): every US stock and ETF -
traded ones and those no longer traded - with its daily prices back to the start of the period of the Plattform-
Einstellungen, unadjusted, its corporate actions apart, the exchanges' calendar, and its Entität by ISIN, ticker or name
- the last uncertain. A point in time sees only what was known then (datamodel.md, rule 11): a day's prices from its
close, a corporate action from the day the source processed it.

The source is Alpaca (app/prices/alpaca.py), while its keys are set. Three chains of jobs, one request each, behind live
work (job.priority) and in free request slots only (fetch_policy_service), each queued by the job before it:
- Wertpapiere abgleichen: the list, exchange by exchange, then the calendar; once a day. Its last step links the
  Wertpapiere without an Entität.
- Kapitalmassnahmen abrufen: the corporate actions processed in ACTION_CHUNK days at a time, page by page, from the
  period's start to yesterday; then each new day.
- Tageskurse abrufen: the daily prices of up to alpaca.BAR_SYMBOLS Wertpapiere at a time, page by page, from where they
  reach to the last closed trading day; begun once the corporate actions are in - so a Wertpapier no longer traded is
  known as such first.
The worker starts a chain every SWEEP minutes when it is due and none of its kind is waiting or running; one that
failed starts again from where it came.
"""
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Connection, Engine

from app.db.models import BACKFILL_PRIORITY, AliasScheme, EntityKind, InstrumentKind, InstrumentStatus, PriceResolution
from app.prices import alpaca
from app.prices.base import Action, Asset
from app.repositories import entity_repository, instrument_repository, job_repository, market_data_repository, price_repository
from app.services import job_service, platform_settings_service
from app.services.entity_names import identifier_key, name_key
from app.services.fetch_policy_service import PoliteHttp
from app.sources.http import HttpClient

logger = logging.getLogger(__name__)

SOURCE = alpaca
ASSETS_SYNC = "assets"  # through: the day of the last complete list
ACTIONS_SYNC = "actions"  # through: the last day whose corporate actions are in
PERIOD_SYNC = "period"  # through: the first day of the period the daily prices were fetched from
SYNC_EVERY = timedelta(hours=20)
ACTION_CHUNK = 90  # days per request: Alpaca allows at most 90
# A day's prices are asked for this long after its close: the free plan does not give the last 15 minutes.
SETTLE = timedelta(minutes=20)
# Alpaca's list has no CUSIP: a Wertpapier takes the one of its own corporate actions processed this recently - an
# older one may be of a company that had its symbol before.
CUSIP_RECENT = timedelta(days=365)
# The daily prices wait for the corporate actions until these come this close to yesterday.
ACTIONS_LEAD = timedelta(days=7)
UPSERT_ROWS = 1000
INSERT_ROWS = 2000
SPLITS = ("forward_split", "reverse_split")
DIVIDENDS = ("cash_dividend",)
# Benchmarks (the admin's decisions of 2026-10-31 and 2026-11-04): broad ETFs, no free source having the indices'
# history - the market's, and per sector of the starter list (its key) one of Alpaca's sector ETFs. Their daily prices
# are fetched before all others' (`instrument_repository.history_due`): an event study measures against them.
MARKET_BENCHMARK = "SPY"
SECTOR_BENCHMARKS = {
    "oil-gas": "XLE",
    "utilities": "XLU",
    "renewable-energy": "ICLN",
    "mining-metals": "XME",
    "chemicals": "XLB",
    "agriculture-food": "PBJ",
    "automotive": "CARZ",
    "aerospace-defense": "ITA",
    "industrial-goods": "XLI",
    "construction-real-estate": "XLRE",
    "transport-logistics": "IYT",
    "airlines-tourism": "JETS",
    "banking": "KBE",
    "insurance": "KIE",
    "financial-services": "XLF",
    "software-it": "IGV",
    "semiconductors": "SMH",
    "hardware-electronics": "XLK",
    "telecommunications": "IYZ",
    "media-entertainment": "XLC",
    "retail-consumer-goods": "XRT",
    "healthcare-pharma": "XLV",
}
BENCHMARK_SYMBOLS = (MARKET_BENCHMARK, *sorted(set(SECTOR_BENCHMARKS.values())))
DELISTINGS = ("worthless_removal", "cash_merger", "stock_merger", "stock_and_cash_merger")
# A ticker as the name forms write it (EXCHANGE:SYMBOL): Alpaca's exchange code, and how reports name it.
TICKER_EXCHANGES = {
    "NASDAQ": ("NASDAQ",), "NYSE": ("NYSE",), "ARCA": ("NYSEARCA", "ARCA"), "AMEX": ("NYSEAMERICAN", "AMEX", "NYSEMKT"),
    "BATS": ("CBOE", "BATS"),
}
# Words after which a listed name names the security, not the company: "Apple Inc. Common Stock".
_SECURITY_WORDS = (
    " common stock", " common shares", " ordinary shares", " class a", " class b", " class c", " american depositary",
    " depositary shares", " ads", " shares of beneficial interest", " units", " capital stock",
)
# A listed name with one of these words is no company's stock: never linked by name.
_NOT_A_COMPANY = (" warrant", " right", " unit", " preferred", " notes", " debenture", " %")


def period_start(today: date, years: int) -> date:
    """The first day of the daily prices: 1 January of the year `years` - 1 before this one (11 in 2026: 2016)."""
    return date(today.year - years + 1, 1, 1)


def isin_of(cusip: str | None) -> str | None:
    """A US CUSIP's ISIN: US, the CUSIP, the check digit."""
    if not cusip or len(cusip) != 9:
        return None
    for digit in "0123456789":
        isin = identifier_key(AliasScheme.ISIN, f"US{cusip}{digit}")
        if isin:
            return isin
    return None


def company_name(listed: str) -> str | None:
    """The company of a listed name - "Apple Inc. Common Stock": "Apple Inc." -, or None for a name that is no
    company's stock (a warrant, a right, a preferred share)."""
    lowered = f"{listed.lower()} "
    if any(word in lowered for word in _NOT_A_COMPANY):
        return None
    cut = min((lowered.find(word) for word in _SECURITY_WORDS if word in lowered), default=len(listed))
    return listed[:cut].strip(" ,-") or None


@dataclass(frozen=True)
class MarketStatus:
    """How far the historical market data came, for the Wertpapiere page."""

    configured: bool  # the source's keys are set
    key_variables: tuple[str, ...]
    period_start: date
    listed_at: datetime | None  # the last complete list and calendar
    actions_through: date | None
    closed_day: date | None  # the last trading day whose prices are due
    total: int  # Wertpapiere with daily prices
    done: int  # of them, those that reach closed_day
    running: bool  # a chain is waiting or running


def status(connection: Connection, now: datetime | None = None) -> MarketStatus:
    now = now or datetime.now(UTC)
    years = platform_settings_service.load(connection).price_history_years
    assets = market_data_repository.sync(connection, ASSETS_SYNC)
    actions = market_data_repository.sync(connection, ACTIONS_SYNC)
    closed = market_data_repository.last_closed_day(connection, now - SETTLE)
    counts = instrument_repository.count_history(connection, SOURCE.NAME, closed)
    running = job_repository.any_active(connection, CHAIN_KINDS)
    return MarketStatus(
        SOURCE.configured(), SOURCE.KEY_VARIABLES, period_start(now.date(), years), assets.synced_at if assets else None,
        actions.through if actions else None, closed, counts.total, counts.done, running,
    )


# The chains

CHAIN_KINDS = (job_service.SYNC_MARKET_JOB_KIND, job_service.FETCH_ACTIONS_JOB_KIND, job_service.FETCH_BARS_JOB_KIND)


def enqueue_due(engine: Engine, now: datetime | None = None) -> list[int]:
    """Start each chain that is due and not under way; none while the source's keys are missing. The worker calls it
    every few minutes."""
    if not SOURCE.configured():
        return []
    now = now or datetime.now(UTC)
    today, queued = now.date(), []
    with engine.begin() as connection:
        start = period_start(today, platform_settings_service.load(connection).price_history_years)
        assets = market_data_repository.sync(connection, ASSETS_SYNC)
        if assets is None or assets.synced_at < now - SYNC_EVERY:
            queued.append(_start(connection, job_service.SYNC_MARKET_JOB_KIND, {"step": 0}))
        if assets is None:
            return [job_id for job_id in queued if job_id]
        period = market_data_repository.sync(connection, PERIOD_SYNC)
        if period is None or period.through > start:
            # The period reaches further back now: everything again from its start; what is stored stays.
            instrument_repository.forget_history(connection, SOURCE.NAME)
            market_data_repository.record_sync(connection, ACTIONS_SYNC, None)
            market_data_repository.record_sync(connection, PERIOD_SYNC, start)
        actions = market_data_repository.sync(connection, ACTIONS_SYNC)
        through = actions.through if actions else None
        yesterday = today - timedelta(days=1)
        if through is None or through < yesterday:
            queued.append(_start(connection, job_service.FETCH_ACTIONS_JOB_KIND, _chunk(through, start, yesterday)))
        closed = market_data_repository.last_closed_day(connection, now - SETTLE)
        if through is not None and through >= yesterday - ACTIONS_LEAD and closed is not None:
            if instrument_repository.history_due(connection, SOURCE.NAME, closed, BENCHMARK_SYMBOLS):
                queued.append(_start(connection, job_service.FETCH_BARS_JOB_KIND, {}))
    return [job_id for job_id in queued if job_id]


def sync_step(engine: Engine, step: int, contact: str) -> Callable[[Connection], None]:
    """First phase of a sync job: one exchange's list, or - the last step - the calendar. The effect stores it and
    queues the next step; the last one also links the Wertpapiere without an Entität."""
    exchanges = SOURCE.EXCHANGES
    today = datetime.now(UTC).date()
    with engine.connect() as connection:
        settings = platform_settings_service.load(connection)
    with HttpClient(contact) as http:
        polite = PoliteHttp(engine, http, settings.domain_min_interval_seconds, backfill=True)
        if step < len(exchanges):
            rows = _asset_rows(SOURCE.assets(polite, exchanges[step]))
            sessions = []
        else:
            rows = []
            sessions = SOURCE.calendar(polite, period_start(today, settings.price_history_years), date(today.year + 1, 12, 31))

    def store(connection: Connection) -> None:
        for begin in range(0, len(rows), UPSERT_ROWS):
            instrument_repository.upsert_listed(connection, rows[begin : begin + UPSERT_ROWS])
        if step < len(exchanges):
            _enqueue(connection, job_service.SYNC_MARKET_JOB_KIND, {"step": step + 1})
            logger.info("Instruments synced", extra={"step": step, "instruments": len(rows)})
            return
        market_data_repository.upsert_trading_days(
            connection, [{"day": each.day, "opens_at": each.opens_at, "closes_at": each.closes_at} for each in sessions]
        )
        market_data_repository.record_sync(connection, ASSETS_SYNC, today)
        linked = link_entities(connection)
        logger.info("Calendar synced", extra={"trading_days": len(sessions), "linked": linked})

    return store


def fetch_actions(engine: Engine, payload: dict, contact: str) -> Callable[[Connection], None]:
    """First phase of a corporate actions job: one page of those processed in its days. The effect stores those of
    known Wertpapiere, marks the ones they end, and queues the next page or the next days."""
    start, end = date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"])
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    with engine.connect() as connection:
        min_interval = platform_settings_service.load(connection).domain_min_interval_seconds
    with HttpClient(contact) as http:
        found, token = SOURCE.corporate_actions(PoliteHttp(engine, http, min_interval, backfill=True), start, end, payload.get("page_token"))

    def store(connection: Connection) -> None:
        rows, delisted, learned = _action_rows(connection, found, yesterday - CUSIP_RECENT)
        for instrument_id, cusip in learned.items():
            instrument_repository.learn_cusip(connection, instrument_id, cusip, isin_of(cusip))
        stored = sum(
            market_data_repository.insert_actions(connection, rows[begin : begin + UPSERT_ROWS]) for begin in range(0, len(rows), UPSERT_ROWS)
        )
        for day, instrument_ids in delisted.items():
            instrument_repository.mark_delisted(connection, instrument_ids, day)
        if token:
            _enqueue(connection, job_service.FETCH_ACTIONS_JOB_KIND, payload | {"page_token": token})
        else:
            market_data_repository.record_sync(connection, ACTIONS_SYNC, end)
            if end < yesterday:
                _enqueue(connection, job_service.FETCH_ACTIONS_JOB_KIND, _chunk(end, start, yesterday))
        logger.info("Corporate actions fetched", extra={"actions": len(found), "matched": len(rows), "new": stored, "done": not token})

    return store


def fetch_bars(engine: Engine, payload: dict, contact: str) -> Callable[[Connection], None] | None:
    """First phase of a daily prices job: one page of a batch's - the batch the job names, or else the next one due.
    The effect stores them and queues the next page, or marks the batch done and queues the next batch. None when
    nothing is due."""
    now = datetime.now(UTC)
    with engine.connect() as connection:
        settings = platform_settings_service.load(connection)
        if payload.get("instruments"):
            instruments, start, end = payload["instruments"], date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"])
            follow_renames, period = payload["follow_renames"], date.fromisoformat(payload["period"])
        else:
            closed = market_data_repository.last_closed_day(connection, now - SETTLE)
            due = instrument_repository.history_due(connection, SOURCE.NAME, closed, BENCHMARK_SYMBOLS) if closed else []
            if not due:
                return None
            batch = next_batch(due)
            instruments = {row.symbol: row.id for row in batch}
            first, period = batch[0], period_start(now.date(), settings.price_history_years)
            start = first.history_through + timedelta(days=1) if first.history_through else period
            end, follow_renames = closed, first.status == InstrumentStatus.ACTIVE
        # A Wertpapier no longer traded: none of its symbol's prices after its last day - another may carry it since.
        last_days = {row.id: row.delisted_on for row in instrument_repository.by_ids(connection, instruments.values())}
    instrument_ids = list(instruments.values())
    if start > end:
        return lambda connection: instrument_repository.set_history_through(connection, instrument_ids, end)
    with HttpClient(contact) as http:
        polite = PoliteHttp(engine, http, settings.domain_min_interval_seconds, backfill=True)
        bars, token = SOURCE.daily_bars(polite, list(instruments), start, end, payload.get("page_token"), follow_renames)
    prices = []
    for bar in bars:
        instrument_id = instruments.get(bar.symbol)
        last_day = last_days.get(instrument_id)
        if instrument_id is None or (last_day is not None and bar.at.astimezone(SOURCE.NEW_YORK).date() > last_day):
            continue
        prices.append({
            "instrument_id": instrument_id, "resolution": PriceResolution.DAY, "at": bar.at, "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": bar.volume, "previous_close": None, "source": SOURCE.NAME, "delayed": False,
        })
    page = {
        "instruments": instruments, "start": start.isoformat(), "end": end.isoformat(), "follow_renames": follow_renames,
        "period": period.isoformat(),
    }

    def store(connection: Connection) -> None:
        stored = sum(
            price_repository.insert_prices(connection, prices[begin : begin + INSERT_ROWS]) for begin in range(0, len(prices), INSERT_ROWS)
        )
        if token:
            _enqueue(connection, job_service.FETCH_BARS_JOB_KIND, page | {"page_token": token})
        else:
            # The period grew while the batch ran: its Wertpapiere stay to be fetched from the new start.
            current = market_data_repository.sync(connection, PERIOD_SYNC)
            if current is None or current.through is None or current.through >= period:
                instrument_repository.set_history_through(connection, instrument_ids, end)
            _enqueue(connection, job_service.FETCH_BARS_JOB_KIND, {})
        logger.info("Daily prices fetched", extra={"instruments": len(instrument_ids), "prices": len(prices), "new": stored, "done": not token})

    return store


def next_batch(due: Sequence) -> list:
    """The first due Wertpapiere that share a start and whether they are traded, each symbol once, at most
    alpaca.BAR_SYMBOLS."""
    first, batch, symbols = due[0], [], set()
    for row in due:
        if row.history_through != first.history_through or row.status != first.status:
            break
        if row.symbol in symbols:
            continue
        symbols.add(row.symbol)
        batch.append(row)
        if len(batch) == SOURCE.BAR_SYMBOLS:
            break
    return batch


def link_entities(connection: Connection) -> int:
    """Link the source's Wertpapiere without an Entität: by ISIN, by ticker - a traded one only, a symbol is reused -
    or by name, then uncertain. The number linked."""
    isins = entity_repository.identifiers(connection, AliasScheme.ISIN)
    tickers = entity_repository.identifiers(connection, AliasScheme.TICKER)
    names = entity_repository.unique_names(connection, (EntityKind.COMPANY, EntityKind.SUBSIDIARY))
    linked = 0
    for row in instrument_repository.unlinked(connection, SOURCE.NAME):
        entity_id, uncertain = isins.get(row.isin or ""), False
        if entity_id is None and row.status == InstrumentStatus.ACTIVE:
            for exchange in TICKER_EXCHANGES.get(row.exchange, (row.exchange,)):
                entity_id = entity_id or tickers.get(f"{exchange}:{row.symbol}")
        if entity_id is None and (company := company_name(row.name)):
            entity_id, uncertain = names.get(name_key(company)), True
        if entity_id is not None:
            instrument_repository.link_entity(connection, row.id, entity_id, uncertain)
            linked += 1
    return linked


def _asset_rows(assets: Iterable[Asset]) -> list[dict]:
    rows = {}
    for asset in assets:
        cusip = asset.cusip if asset.cusip and len(asset.cusip) == 9 else None
        rows[asset.ref] = {
            "source": SOURCE.NAME, "source_ref": asset.ref, "symbol": asset.symbol[:30], "exchange": asset.exchange[:20],
            "name": asset.name, "kind": InstrumentKind.ETF if asset.etf else InstrumentKind.STOCK, "currency": "USD", "cusip": cusip,
            "isin": isin_of(cusip), "status": InstrumentStatus.ACTIVE if asset.active else InstrumentStatus.INACTIVE, "history": True,
            "live": False,
        }
    return list(rows.values())


def _action_rows(
    connection: Connection, found: Iterable[Action], recent: date
) -> tuple[list[dict], dict[date, list[int]], dict[int, str]]:
    """The rows of the corporate actions, one per Wertpapier they concern - by CUSIP, else by its symbol now -, the
    Wertpapiere they end, by their last day, and the CUSIPs Wertpapiere without one take: of an action processed since
    `recent`, matched by symbol."""
    cusips = instrument_repository.cusips(connection, SOURCE.NAME)
    symbols = instrument_repository.symbols(connection, SOURCE.NAME)
    rows, delisted, learned = {}, {}, {}
    for action in found:
        for index, (symbol, cusip) in enumerate(action.parties):
            instrument_id = cusips.get(cusip or "")
            if instrument_id is None:
                instrument_id = symbols.get(symbol or "")
                if instrument_id is not None and cusip and len(cusip) == 9 and action.process_date and action.process_date >= recent:
                    learned.setdefault(instrument_id, cusip)
            if instrument_id is None or (action.ref, instrument_id) in rows:
                continue
            rows[(action.ref, instrument_id)] = {
                "instrument_id": instrument_id, "source": SOURCE.NAME, "source_ref": action.ref[:100], "kind": action.kind,
                "ex_date": action.ex_date, "effective_date": action.effective_date, "process_date": action.process_date,
                "old_rate": action.old_rate, "new_rate": action.new_rate, "rate": action.rate,
                "old_symbol": (action.old_symbol or "")[:30] or None, "new_symbol": (action.new_symbol or "")[:30] or None,
                "currency": (action.currency or "")[:3] or None, "details": action.details,
            }
            # A merger ends the acquired one, its first party; a worthless removal its only one.
            last_day = action.effective_date or action.ex_date or action.process_date
            if action.kind in DELISTINGS and index == 0 and last_day:
                delisted.setdefault(last_day, []).append(instrument_id)
    return list(rows.values()), delisted, learned


def _chunk(through: date | None, start: date, yesterday: date) -> dict:
    begin = through + timedelta(days=1) if through else start
    return {"start": begin.isoformat(), "end": min(begin + timedelta(days=ACTION_CHUNK - 1), yesterday).isoformat()}


def _start(connection: Connection, kind: str, payload: dict) -> int | None:
    """A chain's first job, unless one of its kind is waiting or running."""
    if job_repository.any_active(connection, [kind]):
        return None
    return _enqueue(connection, kind, payload)


def _enqueue(connection: Connection, kind: str, payload: dict) -> int | None:
    # No dedupe key: a follow-up is queued by the effect of the job before it, which runs exactly once.
    return job_service.enqueue(connection, kind, payload, priority=BACKFILL_PRIORITY)


# Point in time


@dataclass(frozen=True)
class KnownBar:
    """A daily price as known at a moment: as received, and split-adjusted with the splits known then."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    factor: Decimal  # the price's adjustment: 1 unless a later split known then

    @property
    def adjusted_close(self) -> Decimal:
        return self.close * self.factor


@dataclass(frozen=True)
class Split:
    ex_date: date
    ratio: Decimal  # old over new: 1/4 for a split of one into four


def split_adjusted(bars: Iterable, splits: Iterable[Split]) -> list[KnownBar]:
    """The bars - day, open, high, low, close, volume - each with the factor of the splits from after its day."""
    splits = sorted(splits, key=lambda each: each.ex_date)
    known = []
    for bar in bars:
        factor = Decimal(1)
        for split in splits:
            if split.ex_date > bar.day:
                factor *= split.ratio
        known.append(KnownBar(bar.day, bar.open, bar.high, bar.low, bar.close, bar.volume, factor))
    return known


def known_on(connection: Connection, instrument_id: int, reference: datetime, limit: int) -> list[KnownBar]:
    """The Wertpapier's latest daily prices as known at `reference`, the latest first: those of the trading days closed
    by then, adjusted for the splits known and in effect by then - processed by the source on `reference`'s day in New
    York or before, and traded split from that day or before."""
    bars = market_data_repository.known_bars(connection, instrument_id, reference, limit)
    reference_day = reference.astimezone(SOURCE.NEW_YORK).date()
    splits = [
        Split(row.ex_date, Decimal(row.old_rate) / Decimal(row.new_rate))
        for row in market_data_repository.actions(connection, instrument_id, SPLITS)
        if row.ex_date and row.ex_date <= reference_day and row.old_rate and row.new_rate
        and (row.process_date or row.ex_date) <= reference_day
    ]
    return split_adjusted(bars, splits)
