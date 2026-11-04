"""Event study (Historische Markt- und Ereignisdaten, story 2; the admin's decisions of 2026-11-04): how the Wertpapiere
of the companies an event's reports name moved around it, against the market (SPY) and the sector its reports name most
(history_service.SECTOR_BENCHMARKS).

- Ereignistag: the first trading day that closes after the event's first report - the first close that can price it.
- Windows in trading days around it, each from the close of the day before its first to the close of its last; returns
  with dividends, adjusted for splits (computed, never stored: datamodel.md, rule 14).
- Two measures, never one score: the return minus the benchmark's (bereinigt), and the abnormal return of the market
  model - alpha and beta of the estimation window -, summed, with its t value. Too few days to estimate: no model.
- A price missing in a window is a gap, named, never filled (requirements.md, Historische Markt- und Ereignisdaten,
  criterion 4); a window not closed yet is open, one not fetched yet pending.
- Periods: an event belongs by its first report's day to training, validation or test (Plattform-Einstellungen). A
  window reaching past its period's end is left out of that period's figures: later prices must not flow into an
  earlier period (criterion 2).

Computed whenever a page asks, nothing stored.
"""
import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Connection, Engine

from app.db.models import PriceSource
from app.repositories import event_study_repository, instrument_repository, market_data_repository
from app.services import platform_settings_service
from app.services.extraction_service import EVENT_TYPE_LABELS
from app.services.history_service import BENCHMARK_SYMBOLS, DIVIDENDS, MARKET_BENCHMARK, SECTOR_BENCHMARKS, SPLITS


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    start: int  # trading days from the Ereignistag (0)
    end: int

    @property
    def days(self) -> int:
        return self.end - self.start + 1


WINDOWS = (
    Window("pre", "Vorlauf (−5 bis −1)", -5, -1),
    Window("day", "Ereignistag", 0, 0),
    Window("d1", "bis +1", 0, 1),
    Window("d5", "bis +5", 0, 5),
    Window("d20", "bis +20", 0, 20),
    Window("d60", "bis +60", 0, 60),
)
# The market model's estimation window: 120 trading days, ending ten before the Ereignistag, so the event's run-up
# does not shape alpha and beta; at least 60 days with both returns, else no model.
ESTIMATION = (-130, -11)
MIN_ESTIMATION_DAYS = 60
# The trading days around the Ereignistag whose closes a measurement reads.
FIRST_OFFSET = ESTIMATION[0] - 1
LAST_OFFSET = max(window.end for window in WINDOWS)


class Period(StrEnum):
    TRAINING = "training"
    VALIDATION = "validation"
    TEST = "test"
    UNDIVIDED = "undivided"


PERIOD_LABELS = {
    Period.TRAINING: "Training",
    Period.VALIDATION: "Validierung",
    Period.TEST: "Test",
    Period.UNDIVIDED: "Nicht aufgeteilt",
}


class Status(StrEnum):
    DONE = "done"
    OPEN = "open"  # its last day has not closed yet
    PENDING = "pending"  # its prices are not fetched yet
    ENDED = "ended"  # the Wertpapier was no longer traded
    GAP = "gap"  # a trading day without a price


STATUS_LABELS = {
    Status.DONE: "gemessen",
    Status.OPEN: "offen",
    Status.PENDING: "ausstehend",
    Status.ENDED: "nicht mehr gehandelt",
    Status.GAP: "Lücke",
}
# What a window of two Wertpapiere is when they differ: the first of these either is.
_SEVERITY = (Status.OPEN, Status.PENDING, Status.ENDED, Status.GAP, Status.DONE)


class Exclusion(StrEnum):
    """Why a measurement does not count in a figure of the overview."""

    OPEN = "open"
    PENDING = "pending"
    ENDED = "ended"
    GAP = "gap"
    NO_MODEL = "no_model"
    CUT = "cut"
    NO_SECTOR = "no_sector"


EXCLUSION_LABELS = {
    Exclusion.OPEN: "offen",
    Exclusion.PENDING: "ausstehend",
    Exclusion.ENDED: "nicht mehr gehandelt",
    Exclusion.GAP: "Lücke",
    Exclusion.NO_MODEL: "zu wenig Daten fürs Marktmodell",
    Exclusion.CUT: "reicht über das Ende des Zeitraums",
    Exclusion.NO_SECTOR: "keine Branche genannt",
}


class Benchmark(StrEnum):
    MARKET = "market"
    SECTOR = "sector"


class Measure(StrEnum):
    MODEL = "model"  # the market model's abnormal return
    EXCESS = "excess"  # return minus the benchmark's


BENCHMARK_LABELS = {Benchmark.MARKET: f"Markt ({MARKET_BENCHMARK})", Benchmark.SECTOR: "Branche"}
MEASURE_LABELS = {Measure.MODEL: "Marktmodell (abnormale Rendite)", Measure.EXCESS: "Bereinigt (Rendite − Benchmark)"}


def percent_label(value: float | None) -> str:
    """+1,23 %; – for none."""
    if value is None:
        return "–"
    shown = f"{value * 100:+.2f}".replace(".", ",")
    return f"{'±0,00' if shown in ('+0,00', '-0,00') else shown} %"


def share_label(value: float | None) -> str:
    """67 %; – for none."""
    return "–" if value is None else f"{value * 100:.0f} %"


def number_label(value: float | None, places: int = 2) -> str:
    return "–" if value is None else f"{value:.{places}f}".replace(".", ",")


# Rules without a database


def period_of(day: date, training_until: date | None, validation_until: date | None) -> Period:
    if training_until is None:
        return Period.UNDIVIDED
    if day <= training_until:
        return Period.TRAINING
    if validation_until is not None and day <= validation_until:
        return Period.VALIDATION
    return Period.TEST


def period_end(period: Period, training_until: date | None, validation_until: date | None) -> date | None:
    """The last day a window of the period may reach; None: no end."""
    return {Period.TRAINING: training_until, Period.VALIDATION: validation_until}.get(period)


def event_index(reported_at: datetime, closes: Sequence[datetime]) -> int | None:
    """The position in the calendar of the first trading day closing after `reported_at`; None beyond the calendar."""
    index = bisect_right(closes, reported_at)
    return index if index < len(closes) else None


@dataclass(frozen=True)
class Split:
    ex_date: date
    ratio: Decimal  # old over new: 1/4 for one share into four


def daily_returns(
    days: Sequence[date], closes: Mapping[date, Decimal], splits: Iterable[Split], dividends: Mapping[date, Decimal]
) -> dict[int, float]:
    """The return of each trading day - by its position in `days` - whose close and the one before are known: with the
    dividend going ex that day, both prices split-adjusted to one basis. A day after a missing close has none."""
    splits = list(splits)

    def factor(day: date) -> Decimal:
        value = Decimal(1)
        for split in splits:
            if split.ex_date > day:
                value *= split.ratio
        return value

    returns = {}
    for index in range(1, len(days)):
        day, before = days[index], days[index - 1]
        if day in closes and before in closes and closes[before]:
            now = (closes[day] + dividends.get(day, Decimal(0))) * factor(day)
            returns[index] = float(now / (closes[before] * factor(before)) - 1)
    return returns


@dataclass(frozen=True)
class Model:
    alpha: float
    beta: float
    sigma: float  # of the residuals
    days: int


def market_model(stock: Mapping[int, float], benchmark: Mapping[int, float], indices: Iterable[int]) -> Model | None:
    """Ordinary least squares of the stock's returns on the benchmark's, over the days of `indices` with both."""
    pairs = [(benchmark[index], stock[index]) for index in indices if index in stock and index in benchmark]
    if len(pairs) < MIN_ESTIMATION_DAYS:
        return None
    xs, ys = [x for x, _ in pairs], [y for _, y in pairs]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return None
    beta = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / variance
    alpha = mean_y - beta * mean_x
    residuals = sum((y - alpha - beta * x) ** 2 for x, y in pairs)
    return Model(alpha, beta, math.sqrt(residuals / (len(pairs) - 2)), len(pairs))


@dataclass(frozen=True)
class Series:
    """A Wertpapier's prices as a measurement reads them."""

    instrument_id: int
    symbol: str
    name: str
    history_through: date | None
    delisted_on: date | None
    closes: Mapping[date, Decimal]
    returns: Mapping[int, float]


def window_status(series: Series, days: Sequence[date], first: int, last: int, now: datetime, closes_at: Sequence[datetime]) -> tuple[Status, list[date]]:
    """Whether the closes from position `first` to `last` are all there, and the days missing."""
    if first < 0:
        # Before the calendar: no price was ever fetched for it.
        return Status.GAP, []
    if last >= len(days) or closes_at[last] > now:
        return Status.OPEN, []
    missing = [days[index] for index in range(first, last + 1) if days[index] not in series.closes]
    if not missing:
        return Status.DONE, []
    if series.history_through is None or missing[-1] > series.history_through:
        return Status.PENDING, missing
    if series.delisted_on is not None and missing[-1] > series.delisted_on:
        return Status.ENDED, missing
    return Status.GAP, missing


@dataclass(frozen=True)
class WindowResult:
    window: Window
    first_day: date | None
    last_day: date | None
    status: Status
    missing: list[tuple[str, date]]  # symbol, day
    raw: float | None = None  # the Wertpapier's return
    benchmark: float | None = None  # the benchmark's
    excess: float | None = None  # raw minus benchmark
    abnormal: float | None = None  # the market model's, summed
    t: float | None = None
    cut: bool = False  # reaches past the end of the event's period

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    def value(self, measure: Measure) -> float | None:
        return self.abnormal if measure == Measure.MODEL else self.excess


def measure_window(
    window: Window, start: int, stock: Series, benchmark: Series, model: Model | None, days: Sequence[date],
    closes_at: Sequence[datetime], now: datetime, end_of_period: date | None,
) -> WindowResult:
    """One window of the Wertpapier against one benchmark, the Ereignistag at position `start`."""
    first, last = start + window.start, start + window.end
    first_day = days[first] if 0 <= first < len(days) else None
    last_day = days[last] if 0 <= last < len(days) else None
    cut = end_of_period is not None and (last_day is None or last_day > end_of_period)
    statuses = [window_status(series, days, first - 1, last, now, closes_at) for series in (stock, benchmark)]
    status = min((each for each, _ in statuses), key=_SEVERITY.index)
    missing = [(series.symbol, day) for series, (_, days_missing) in zip((stock, benchmark), statuses) for day in days_missing]
    if status != Status.DONE:
        return WindowResult(window, first_day, last_day, status, missing, cut=cut)
    indices = range(first, last + 1)
    raw = math.prod(1 + stock.returns[index] for index in indices) - 1
    bench = math.prod(1 + benchmark.returns[index] for index in indices) - 1
    abnormal = t = None
    if model is not None:
        abnormal = sum(stock.returns[index] - model.alpha - model.beta * benchmark.returns[index] for index in indices)
        t = abnormal / (model.sigma * math.sqrt(window.days)) if model.sigma else None
    return WindowResult(window, first_day, last_day, status, [], raw, bench, raw - bench, abnormal, t, cut)


# Views


@dataclass(frozen=True)
class SectorRef:
    key: str
    name: str
    symbol: str  # its benchmark ETF


@dataclass(frozen=True)
class Adjustment:
    symbol: str
    day: date
    text: str  # "Split 1:4", "Dividende 0,24 USD"


@dataclass(frozen=True)
class Measurement:
    """A Wertpapier's reaction against one benchmark."""

    benchmark: Benchmark
    symbol: str
    name: str
    model: Model | None
    windows: list[WindowResult]
    pending: bool = False  # none of the benchmark's daily prices around the event are fetched yet

    @property
    def benchmark_label(self) -> str:
        return "Markt" if self.benchmark == Benchmark.MARKET else "Branche"

    @property
    def model_label(self) -> str:
        if self.model is None and self.pending:
            return f"Marktmodell: ausstehend, {self.symbol} hat noch keine Tageskurse"
        if self.model is None:
            return f"Marktmodell: zu wenig Daten (weniger als {MIN_ESTIMATION_DAYS} Handelstage mit beiden Kursen im Schätzfenster)"
        return (
            f"Marktmodell aus {self.model.days} Handelstagen: Alpha {percent_label(self.model.alpha)} pro Tag, "
            f"Beta {number_label(self.model.beta)}"
        )


@dataclass(frozen=True)
class Reaction:
    """One Wertpapier of the event's companies."""

    instrument_id: int
    symbol: str
    name: str
    measurements: list[Measurement]
    adjustments: list[Adjustment]
    gaps: list[date]  # its trading days without a price, from the estimation window's start to the last window's end


@dataclass(frozen=True)
class EventReaction:
    """The event page's Marktreaktion."""

    event_day: date | None
    estimation: tuple[date, date] | None
    period: Period
    sector: SectorRef | None
    reactions: list[Reaction]
    benchmarks_missing: list[str]  # benchmark symbols not in Alpaca's list

    @property
    def period_label(self) -> str:
        return PERIOD_LABELS[self.period]


@dataclass
class Figure:
    """The figures of one cell of the overview."""

    values: list[float] = field(default_factory=list)
    excluded: dict[Exclusion, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.values) if self.values else None

    @property
    def median(self) -> float | None:
        return statistics.median(self.values) if self.values else None

    @property
    def positive(self) -> float | None:
        return sum(value > 0 for value in self.values) / len(self.values) if self.values else None

    @property
    def excluded_label(self) -> str:
        return ", ".join(f"{EXCLUSION_LABELS[reason]} {self.excluded[reason]}" for reason in Exclusion if self.excluded.get(reason))

    @property
    def excluded_count(self) -> int:
        return sum(self.excluded.values())


@dataclass(frozen=True)
class OverviewRow:
    event_type: str | None  # None: all types
    label: str
    window: Window
    figures: dict[Period, Figure]


@dataclass(frozen=True)
class Overview:
    benchmark: Benchmark
    measure: Measure
    periods: list[Period]
    rows: list[OverviewRow]
    events: int
    observations: int
    training_until: date | None
    validation_until: date | None
    benchmarks_missing: list[str]

    @property
    def period_labels(self) -> dict[Period, str]:
        return PERIOD_LABELS


# Computation


@dataclass(frozen=True)
class _Observation:
    event_id: int
    event_type: str
    start: int  # the Ereignistag's position in the calendar
    period: Period
    reaction: Reaction


def _action_lines(rows) -> tuple[dict[int, list[Split]], dict[int, dict[date, Decimal]], dict[int, list[tuple[date, str]]]]:
    splits, dividends, lines = defaultdict(list), defaultdict(lambda: defaultdict(Decimal)), defaultdict(list)
    for row in rows:
        if row.ex_date is None:
            continue
        if row.kind in SPLITS and row.old_rate and row.new_rate:
            splits[row.instrument_id].append(Split(row.ex_date, Decimal(row.old_rate) / Decimal(row.new_rate)))
            old, new = (f"{Decimal(rate).normalize():f}" for rate in (row.old_rate, row.new_rate))
            lines[row.instrument_id].append((row.ex_date, f"{'Aktiensplit' if row.kind == 'forward_split' else 'Zusammenlegung'} {old}:{new}"))
        elif row.kind in DIVIDENDS and row.rate:
            dividends[row.instrument_id][row.ex_date] += Decimal(row.rate)
            amount = f"{Decimal(row.rate):.4f}".rstrip("0").rstrip(".").replace(".", ",")
            lines[row.instrument_id].append((row.ex_date, f"Dividende {amount} {row.currency or 'USD'}"))
    return splits, dividends, lines


def _study(connection: Connection, rows: Sequence, now: datetime) -> tuple[list[_Observation], dict, list[str], dict]:
    """Every observation of the rows (event_study_repository.observations) measured against the market and the event's
    sector; the calendar's positions as context."""
    settings = platform_settings_service.load(connection)
    calendar = market_data_repository.trading_days(connection)
    days = [row.day for row in calendar]
    closes_at = [row.closes_at for row in calendar]
    benchmarks = instrument_repository.by_symbols(connection, PriceSource.ALPACA, BENCHMARK_SYMBOLS)
    missing_benchmarks = [symbol for symbol in BENCHMARK_SYMBOLS if symbol not in benchmarks]
    event_ids = sorted({row.event_id for row in rows})
    sectors = _sectors(connection, event_ids)

    starts = {row.event_id: event_index(row.first_reported_at, closes_at) for row in rows}
    spans: dict[int, list[int]] = {}

    def widen(instrument_id: int, start: int) -> None:
        first, last = max(start + FIRST_OFFSET, 0), min(start + LAST_OFFSET, len(days) - 1)
        span = spans.setdefault(instrument_id, [first, last])
        span[0], span[1] = min(span[0], first), max(span[1], last)

    for row in rows:
        start = starts[row.event_id]
        if start is None:
            continue
        widen(row.instrument_id, start)
        for symbol in (MARKET_BENCHMARK, sectors[row.event_id].symbol if sectors.get(row.event_id) else None):
            if symbol in benchmarks:
                widen(benchmarks[symbol].id, start)

    closes: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for bar in market_data_repository.daily_closes(connection, [(key, days[a], days[b]) for key, (a, b) in spans.items()]):
        closes[bar.instrument_id][bar.day] = bar.close
    splits, dividends, lines = _action_lines(market_data_repository.actions_of(connection, spans, (*SPLITS, *DIVIDENDS)))

    series_cache: dict[int, Series] = {}

    def series(key: int, instrument) -> Series:
        if key not in series_cache:
            returns = daily_returns(days, closes[key], splits[key], dividends[key])
            series_cache[key] = Series(
                key, instrument.symbol, instrument.name, instrument.history_through, instrument.delisted_on, closes[key], returns
            )
        return series_cache[key]

    observations = []
    for row in rows:
        start = starts[row.event_id]
        reported = row.first_reported_at.astimezone(UTC).date()
        period = period_of(reported, settings.study_training_until, settings.study_validation_until)
        end_of_period = period_end(period, settings.study_training_until, settings.study_validation_until)
        if start is None:
            observations.append(_Observation(row.event_id, row.event_type, -1, period, Reaction(row.instrument_id, row.symbol, row.name, [], [], [])))
            continue
        stock = series(row.instrument_id, row)
        measurements = []
        sector = sectors.get(row.event_id)
        for kind, symbol in ((Benchmark.MARKET, MARKET_BENCHMARK), (Benchmark.SECTOR, sector.symbol if sector else None)):
            if symbol not in benchmarks:
                continue
            bench = series(benchmarks[symbol].id, benchmarks[symbol])
            model = market_model(stock.returns, bench.returns, range(start + ESTIMATION[0], start + ESTIMATION[1] + 1))
            windows = [measure_window(window, start, stock, bench, model, days, closes_at, now, end_of_period) for window in WINDOWS]
            measurements.append(Measurement(kind, symbol, bench.name, model, windows, not bench.closes))
        first, last = max(start + FIRST_OFFSET, 0), min(start + LAST_OFFSET, len(days) - 1)
        involved = [row.instrument_id] + [benchmarks[each.symbol].id for each in measurements]
        symbols = {row.instrument_id: row.symbol} | {benchmarks[each.symbol].id: each.symbol for each in measurements}
        adjustments = sorted(
            (Adjustment(symbols[key], day, text) for key in involved for day, text in lines[key] if days[first] <= day <= days[last]),
            key=lambda each: (each.day, each.symbol),
        )
        # From its first price on - before it, it was not listed yet - up to where its prices are fetched.
        listed = min((day for day in stock.closes if day >= days[first]), default=None)
        reached = min(days[last], stock.history_through) if stock.history_through else None
        gaps = [day for day in days[first : last + 1] if listed and reached and listed <= day <= reached and day not in stock.closes]
        reaction = Reaction(row.instrument_id, row.symbol, row.name, measurements, adjustments, gaps)
        observations.append(_Observation(row.event_id, row.event_type, start, period, reaction))
    context = {"days": days, "settings": settings, "sectors": sectors}
    return observations, context, missing_benchmarks, starts


def _sectors(connection: Connection, event_ids: Sequence[int]) -> dict[int, SectorRef]:
    """Per event the sector its reports name most - with a benchmark -, on a tie the first by key."""
    best: dict[int, tuple] = {}
    for row in event_study_repository.sectors(connection, event_ids) if event_ids else []:
        if row.key not in SECTOR_BENCHMARKS:
            continue
        rank = (-row.reports, row.key)
        if row.event_id not in best or rank < best[row.event_id][0]:
            best[row.event_id] = (rank, SectorRef(row.key, row.name, SECTOR_BENCHMARKS[row.key]))
    return {event_id: ref for event_id, (_, ref) in best.items()}


def of_event(engine: Engine, event_id: int, now: datetime | None = None) -> EventReaction:
    """The Marktreaktion of the Wertpapiere of the companies the event's reports name."""
    now = now or datetime.now(UTC)
    with engine.connect() as connection:
        rows = event_study_repository.observations(connection, [event_id])
        observations, context, missing, starts = _study(connection, rows, now)
        sector = _sectors(connection, [event_id]).get(event_id)
        reported_at = event_study_repository.first_reported_at(connection, event_id)
    settings, days = context["settings"], context["days"]
    period = (
        period_of(reported_at.astimezone(UTC).date(), settings.study_training_until, settings.study_validation_until)
        if reported_at else Period.UNDIVIDED
    )
    start = starts.get(event_id)
    event_day = estimation = None
    if start is not None:
        event_day = days[start]
        if start + ESTIMATION[0] >= 0:
            estimation = (days[start + ESTIMATION[0]], days[start + ESTIMATION[1]])
    return EventReaction(event_day, estimation, period, sector, [each.reaction for each in observations], missing)


@dataclass(frozen=True)
class PreMove:
    """A Wertpapier's Vorlauf before an event against the market: whether the event was priced in (impact_service)."""

    instrument_id: int
    symbol: str
    status: Status
    abnormal: float | None  # the market model's, summed over the Vorlauf
    t: float | None


def pre_moves(connection: Connection, event_id: int, entity_ids: Iterable[int], now: datetime | None = None) -> dict[int, PreMove]:
    """Per entity the Vorlauf (-5 to -1) of its Wertpapier with daily prices - a traded one first, then by symbol -
    against SPY, by the market model; an entity without one is left out."""
    entity_ids = list(entity_ids)
    if not entity_ids:
        return {}
    rows = event_study_repository.observations_of_entities(connection, event_id, entity_ids)
    if not rows:
        return {}
    observations, _, _, _ = _study(connection, rows, now or datetime.now(UTC))
    moves: dict[int, PreMove] = {}
    for row, observation in zip(rows, observations, strict=True):
        market = next((each for each in observation.reaction.measurements if each.benchmark == Benchmark.MARKET), None)
        if row.entity_id in moves or market is None:
            continue
        before = market.windows[0]
        moves[row.entity_id] = PreMove(row.instrument_id, row.symbol, before.status, before.abnormal, before.t)
    return moves


def sectors_of(connection: Connection, event_ids: Iterable[int]) -> dict[int, SectorRef]:
    """Per event the sector its reports name most, with its benchmark ETF (backtest_service)."""
    return _sectors(connection, list(event_ids))


def splits_and_dividends(rows) -> tuple[dict[int, list[Split]], dict[int, dict[date, Decimal]]]:
    """Corporate action rows as `daily_returns` reads them, by Wertpapier (backtest_service)."""
    splits, dividends, _ = _action_lines(rows)
    return splits, dividends


def sector_of(connection: Connection, event_id: int) -> SectorRef | None:
    """The sector the event's reports name most, with its benchmark ETF (forecast_service)."""
    return _sectors(connection, [event_id]).get(event_id)


@dataclass(frozen=True)
class Calendar:
    """The trading days with their closes, loaded once for many measurements."""

    days: list[date]
    closes_at: list[datetime]

    @property
    def index(self) -> dict[date, int]:
        return {day: number for number, day in enumerate(self.days)}

    def last_closed(self, moment: datetime) -> int | None:
        """The position of the last trading day closed at `moment`; None before the calendar."""
        position = bisect_right(self.closes_at, moment) - 1
        return position if position >= 0 else None


def calendar(connection: Connection) -> Calendar:
    rows = market_data_repository.trading_days(connection)
    return Calendar([row.day for row in rows], [row.closes_at for row in rows])


def measure_after(
    connection: Connection, days: Calendar, stock, benchmark, entry_day: date, horizon: int, now: datetime,
) -> tuple[WindowResult | None, list[Adjustment]]:
    """The Wertpapier against the benchmark from the close of `entry_day` to that `horizon` trading days later, as a
    window of the event study (returns with dividends, split-adjusted; a missing close a gap), and the splits and
    dividends in it. `stock` and `benchmark`: rows with id, symbol, name, history_through, delisted_on."""
    start = days.index.get(entry_day)
    if start is None:
        return None, []
    last = min(start + horizon, len(days.days) - 1)
    spans = [(each.id, days.days[start], days.days[last]) for each in (stock, benchmark)]
    closes: dict[int, dict[date, Decimal]] = defaultdict(dict)
    for bar in market_data_repository.daily_closes(connection, spans):
        closes[bar.instrument_id][bar.day] = bar.close
    splits, dividends, lines = _action_lines(market_data_repository.actions_of(connection, [stock.id, benchmark.id], (*SPLITS, *DIVIDENDS)))
    series = [
        Series(each.id, each.symbol, each.name, each.history_through, each.delisted_on, closes[each.id],
               daily_returns(days.days, closes[each.id], splits[each.id], dividends[each.id]))
        for each in (stock, benchmark)
    ]
    window = Window("forecast", f"{horizon} Handelstage", 1, horizon)
    result = measure_window(window, start, *series, None, days.days, days.closes_at, now, None)
    adjustments = sorted(
        (Adjustment(each.symbol, day, text) for each in (stock, benchmark) for day, text in lines[each.id]
         if days.days[start] < day <= days.days[last]),
        key=lambda each: (each.day, each.symbol),
    )
    return result, adjustments


def parse_choice(params: Mapping[str, str]) -> tuple[Benchmark, Measure]:
    """The overview's choice from its URL; anything else is the default: market, market model."""
    benchmark = params.get("benchmark", "")
    measure = params.get("measure", "")
    return (
        Benchmark(benchmark) if benchmark in Benchmark._value2member_map_ else Benchmark.MARKET,
        Measure(measure) if measure in Measure._value2member_map_ else Measure.MODEL,
    )


def overview(engine: Engine, benchmark: Benchmark = Benchmark.MARKET, measure: Measure = Measure.MODEL, now: datetime | None = None) -> Overview:
    """Per event type and window, per period: the measurements' mean, median and share above zero, and what was left
    out and why. A Wertpapier named by an event counts once for it."""
    now = now or datetime.now(UTC)
    with engine.connect() as connection:
        rows = event_study_repository.observations(connection)
        observations, context, missing, _ = _study(connection, rows, now)
    settings = context["settings"]
    periods = (
        [Period.UNDIVIDED]
        if settings.study_training_until is None
        else [Period.TRAINING, *([Period.VALIDATION] if settings.study_validation_until else []), Period.TEST]
    )
    types = sorted({each.event_type for each in observations}, key=lambda each: EVENT_TYPE_LABELS.get(each, each))
    table: dict[tuple[str | None, str], dict[Period, Figure]] = {}
    for key in [None, *types]:
        for window in WINDOWS:
            table[(key, window.key)] = {period: Figure() for period in periods}
    for each in observations:
        if each.start < 0:
            continue
        found = next((m for m in each.reaction.measurements if m.benchmark == benchmark), None)
        for window_index, window in enumerate(WINDOWS):
            for key in (None, each.event_type):
                figure = table[(key, window.key)][each.period]
                if found is None:
                    figure.excluded[Exclusion.NO_SECTOR if benchmark == Benchmark.SECTOR else Exclusion.PENDING] += 1
                    continue
                result = found.windows[window_index]
                reason = _exclusion(result, measure)
                if reason is None:
                    figure.values.append(result.value(measure))
                else:
                    figure.excluded[reason] += 1
    result_rows = [
        OverviewRow(key, "Alle Typen" if key is None else EVENT_TYPE_LABELS.get(key, key), window, table[(key, window.key)])
        for key in [None, *types]
        for window in WINDOWS
    ]
    return Overview(
        benchmark, measure, periods, result_rows, len({each.event_id for each in observations}), len(observations),
        settings.study_training_until, settings.study_validation_until, missing,
    )


def _exclusion(result: WindowResult, measure: Measure) -> Exclusion | None:
    if result.status != Status.DONE:
        return Exclusion(result.status.value)
    if result.cut:
        return Exclusion.CUT
    if result.value(measure) is None:
        return Exclusion.NO_MODEL
    return None
