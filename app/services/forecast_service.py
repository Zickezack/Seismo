"""Prognosen (userstories.md, Prognoseprotokoll und Evaluation, story 1; the admin's decisions of 2026-11-04): what
the platform forecast, logged before its outcome is known and never changed (datamodel.md, rule 12), its outcome
stored apart once known.

- Logged with every new version of a Wirkungsannahme - proposed or confirmed, with a probability, positive or
  negative - in the version's transaction: per horizon (5, 20, 60 trading days) one for the target's Wertpapier with
  daily prices (Alpaca's, a traded one). A version that repeats the hypothesis's last forecast - its direction and
  probability, as a confirmation does - logs none.
- The target metric: the Wertpapier's return with dividends, split-adjusted, minus the benchmark's - the ETF of the
  sector the event names most, else SPY - from the close of the last trading day closed when it was logged to that
  `horizon` trading days later. So the whole window lies after the forecast, whenever the event was.
- The outcome: once the last day has closed and the prices are in, measured as an event study's window; a missing
  close is a gap - not measurable, never filled.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Connection, Engine

from app.db.models import FORECAST_HORIZONS, ImpactStatus, OutcomeStatus, PriceSource
from app.repositories import event_study_repository, forecast_repository, instrument_repository
from app.services import event_study_service
from app.services.event_study_service import Status, percent_label, share_label
from app.services.extraction_service import DIRECTION_LABELS
from app.services.history_service import MARKET_BENCHMARK

logger = logging.getLogger(__name__)

RESOLVE_BATCH = 200
LIST_LIMIT = 200
OUTCOME_LABELS = {
    OutcomeStatus.MEASURED: "gemessen",
    OutcomeStatus.GAP: "nicht messbar: Lücke",
    OutcomeStatus.ENDED: "nicht messbar: nicht mehr gehandelt",
}
PROBABILITY_LABELS = {"high": "hoch", "medium": "mittel", "low": "niedrig"}
BENCHMARK_LABELS = {"market": "Markt", "sector": "Branche"}


def log_version(connection: Connection, version_id: int, now: datetime | None = None) -> list[int]:
    """The forecasts a new version of a Wirkungsannahme makes, inside the caller's transaction; their ids."""
    now = now or datetime.now(UTC)
    row = forecast_repository.version(connection, version_id)
    if (
        row is None or row.status not in (ImpactStatus.PROPOSED, ImpactStatus.CONFIRMED) or row.probability is None
        or row.direction not in ("positive", "negative")
    ):
        return []
    latest = forecast_repository.latest_of_hypothesis(connection, row.hypothesis_id)
    if latest is not None and (latest.direction, latest.probability) == (row.direction, row.probability):
        return []
    targets = [
        each for each in event_study_repository.observations_of_entities(connection, row.event_id, [row.target_entity_id])
        if each.delisted_on is None
    ]
    if not targets:
        return []
    asset = targets[0]
    sector = event_study_service.sector_of(connection, row.event_id)
    found = instrument_repository.by_symbols(connection, PriceSource.ALPACA, [MARKET_BENCHMARK, *([sector.symbol] if sector else [])])
    benchmark, kind = (found[sector.symbol], "sector") if sector and sector.symbol in found else (found.get(MARKET_BENCHMARK), "market")
    if benchmark is None or benchmark.id == asset.instrument_id:
        return []
    days = event_study_service.calendar(connection)
    entry = days.last_closed(now)
    if entry is None:
        return []
    rows = [
        {
            "hypothesis_version_id": row.id, "hypothesis_id": row.hypothesis_id, "event_id": row.event_id,
            "instrument_id": asset.instrument_id, "benchmark_instrument_id": benchmark.id, "benchmark": kind,
            "sector_key": sector.key if kind == "sector" else None, "horizon": horizon, "direction": row.direction,
            "probability": row.probability, "classification": row.classification, "entry_day": days.days[entry],
            "end_day": days.days[entry + horizon], "prices_through": asset.history_through,
            "benchmark_prices_through": benchmark.history_through, "origin": row.origin, "model": row.model,
            "prompt_version": row.prompt_version, "created_at": now,
        }
        for horizon in FORECAST_HORIZONS
        # The calendar reaches the end of next year: a horizon beyond it is not logged.
        if entry + horizon < len(days.days)
    ]
    ids = forecast_repository.insert_forecasts(connection, rows)
    if ids:
        logger.info("Forecasts logged", extra={"hypothesis_id": row.hypothesis_id, "version_id": row.id, "forecasts": len(ids)})
    return ids


def resolve_due(engine: Engine, now: datetime | None = None) -> int:
    """Store the outcome of every forecast whose horizon has closed and whose prices are in; how many."""
    now = now or datetime.now(UTC)
    with engine.begin() as connection:
        due = forecast_repository.due(connection, now, RESOLVE_BATCH)
        if not due:
            return 0
        days = event_study_service.calendar(connection)
        stored = 0
        for row in due:
            asset = _Side(row.asset_id, row.asset_symbol, row.asset_name, row.asset_history_through, row.asset_delisted_on)
            benchmark = _Side(row.benchmark_id, row.benchmark_symbol, row.benchmark_name, row.benchmark_history_through, row.benchmark_delisted_on)
            result, adjustments = event_study_service.measure_after(connection, days, asset, benchmark, row.entry_day, row.horizon, now)
            values = outcome(row.direction, result)
            if values is None:
                continue
            values |= {
                "forecast_id": row.id,
                "adjustments": [{"symbol": each.symbol, "day": each.day.isoformat(), "text": each.text} for each in adjustments],
            }
            stored += forecast_repository.insert_outcome(connection, values)
    if stored:
        logger.info("Forecast outcomes stored", extra={"outcomes": stored})
    return stored


@dataclass(frozen=True)
class _Side:
    id: int
    symbol: str
    name: str
    history_through: date | None
    delisted_on: date | None


def outcome(direction: str, result) -> dict | None:
    """The outcome a measured window makes; None while it is not due (open, pending, or beyond the calendar)."""
    if result is None or result.status in (Status.OPEN, Status.PENDING):
        return None
    missing = [{"symbol": symbol, "day": day.isoformat()} for symbol, day in result.missing]
    if result.status == Status.GAP:
        return {"status": OutcomeStatus.GAP, "missing": missing}
    if result.status == Status.ENDED:
        return {"status": OutcomeStatus.ENDED, "missing": missing}
    hit = result.excess > 0 if direction == "positive" else result.excess < 0
    return {
        "status": OutcomeStatus.MEASURED, "asset_return": result.raw, "benchmark_return": result.benchmark,
        "excess_return": result.excess, "hit": hit, "missing": [],
    }


# Views


@dataclass(frozen=True)
class ForecastView:
    id: int
    event_id: int
    event_title: str
    hypothesis_id: int
    target_id: int
    target_name: str
    instrument_id: int
    asset_symbol: str
    asset_name: str
    benchmark_symbol: str
    benchmark: str
    horizon: int
    direction: str
    probability: str
    classification: str
    entry_day: date
    end_day: date
    prices_through: date | None
    origin: str
    model: str | None
    prompt_version: int | None
    created_at: datetime
    outcome_status: str | None
    asset_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    hit: bool | None
    missing: list
    recorded_at: datetime | None

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS[self.direction]

    @property
    def probability_label(self) -> str:
        return PROBABILITY_LABELS[self.probability]

    @property
    def benchmark_label(self) -> str:
        return f"{BENCHMARK_LABELS[self.benchmark]} ({self.benchmark_symbol})"

    @property
    def made_by(self) -> str:
        return f"LLM ({self.model}, Prompt {self.prompt_version})" if self.model else "Analyst"

    @property
    def outcome_label(self) -> str:
        if self.outcome_status is None:
            return f"offen bis {self.end_day.strftime('%d.%m.%Y')}"
        if self.outcome_status != OutcomeStatus.MEASURED:
            return OUTCOME_LABELS[self.outcome_status]
        return f"{percent_label(self.excess_return)} gegen {self.benchmark_symbol}: {'Treffer' if self.hit else 'kein Treffer'}"

    @property
    def returns_label(self) -> str | None:
        if self.outcome_status != OutcomeStatus.MEASURED:
            return None
        return f"{self.asset_symbol} {percent_label(self.asset_return)}, {self.benchmark_symbol} {percent_label(self.benchmark_return)}"

    @property
    def missing_label(self) -> str | None:
        if not self.missing:
            return None
        shown = ", ".join(f"{each['symbol']} {date.fromisoformat(each['day']).strftime('%d.%m.%Y')}" for each in self.missing[:3])
        return f"ohne Kurs: {shown}" + (f" und {len(self.missing) - 3} weitere" if len(self.missing) > 3 else "")


def _view(row) -> ForecastView:
    names = ForecastView.__dataclass_fields__
    return ForecastView(**{name: value for name, value in row._mapping.items() if name in names})


@dataclass(frozen=True)
class HitRate:
    horizon: int
    probability: str
    count: int
    hits: int
    mean_signed: float | None

    @property
    def probability_label(self) -> str:
        return PROBABILITY_LABELS[self.probability]

    @property
    def rate_label(self) -> str:
        return share_label(self.hits / self.count) if self.count else "–"

    @property
    def mean_label(self) -> str:
        return percent_label(self.mean_signed)


@dataclass(frozen=True)
class Overview:
    forecasts: list[ForecastView]
    open: int
    measured: int
    unmeasured: int
    rates: list[HitRate]
    status: str  # "", "open" or "resolved"


STATUSES = {"": None, "open": False, "resolved": True}


def overview(engine: Engine, status: str = "") -> Overview:
    """The latest forecasts, open or resolved as chosen, the counts, and per horizon and probability the hit rate of
    the measured ones - counts beside each other, no single score."""
    status = status if status in STATUSES else ""
    with engine.connect() as connection:
        rows = forecast_repository.listing(connection, STATUSES[status], LIST_LIMIT)
        counts = forecast_repository.counts(connection)
        rates = forecast_repository.hit_rates(connection)
    order = {"high": 0, "medium": 1, "low": 2}
    hit_rates = sorted(
        (HitRate(row.horizon, row.probability, row.count, row.hits, row.mean_signed) for row in rates),
        key=lambda each: (each.horizon, order[each.probability]),
    )
    return Overview([_view(row) for row in rows], counts.open, counts.measured, counts.unmeasured, hit_rates, status)


def of_hypothesis(engine: Engine, hypothesis_id: int) -> list[ForecastView]:
    with engine.connect() as connection:
        return [_view(row) for row in forecast_repository.of_hypotheses(connection, [hypothesis_id])]
