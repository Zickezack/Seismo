"""Backtests of the Wirkungsannahmen (userstories.md, Prognoseprotokoll und Evaluation, story 2; the admin's decisions
of 2026-11-04): what trading their signals would have earned after costs, against simple baselines, so a model is never
judged on its in-sample results alone, and paper trading comes before any real trading (requirements.md, Prognoseprotokoll
und Evaluation, criteria 2 to 5).

- Signals: every version of a Wirkungsannahme that says a direction with a probability - proposed or confirmed,
  positive or negative -, for its target's US Wertpapier with daily prices; a version repeating its hypothesis's last
  signal (a confirmation) is none - as a Prognose is logged. Known when the version was created; the mode "ab
  Ereignistag" takes each hypothesis's first signal from the event's first report on, with hindsight - always flagged as
  data leakage, never judged.
- Execution: from the open of the first trading day opening after the signal to the close of the horizon's last
  trading day, long or short the Wertpapier against the benchmark - the ETF of the sector the event names most, else
  SPY -, returns with dividends (one going ex on the entry day is not the buyer's), split-adjusted. Costs per trade: on
  both legs at entry and exit the run's basis points, and the short leg's borrow fee over its days. A missing price makes
  a trade not measurable, never filled.
- Baselines on the same trades and costs: always long, a direction by chance (fixed per trade), and momentum - the
  direction of the excess return of the 20 trading days before the signal.
- Figures side by side, never one score: trades, hits, mean net return with its t value, sum, maximal drawdown of the
  sum by exit day; per period (training, validation, test - a trade reaching past its period's end does not count
  there), per market phase at the entry (SPY over or under its 200-day mean, its 20-day volatility over or under the
  median of the year before), and paper trading: the logged Prognosen, with the same rules.
- Reproducible: a run reads only prices and corporate actions received by its data state and versions created by
  then, stores its parameters, periods, code version and its trades, and is never changed - the database refuses it,
  so no variant tried can be hidden. A run again with the same data state shows whether it came out the same. Which
  Wertpapier and sector belong to a target and an event is read as now: a correction since shows as a difference.
- Judged on the test period only, against a threshold that rises with the variants tried on it (Bonferroni), and with
  enough paper trading; Seismo itself never trades.
"""
import hashlib
import json
import logging
import math
import random
import statistics
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from statistics import NormalDist

from sqlalchemy import Connection, Engine

from app.db.models import (
    BACKFILL_PRIORITY,
    FORECAST_HORIZONS,
    BacktestMode,
    PriceSource,
    TradeKind,
    TradeStatus,
)
from app.repositories import backtest_repository, instrument_repository
from app.services import event_study_service, job_service, platform_settings_service
from app.services.event_study_service import PERIOD_LABELS, Period, number_label, percent_label, period_end, period_of, share_label
from app.services.extraction_service import DIRECTION_LABELS
from app.services.forecast_service import PROBABILITY_LABELS
from app.services.history_service import BENCHMARK_SYMBOLS, DIVIDENDS, MARKET_BENCHMARK, SPLITS
from app.services.job_service import RUN_BACKTEST_JOB_KIND
from app.services.platform_settings_service import InvalidSetting

logger = logging.getLogger(__name__)

# A change of what a run computes is a new version: runs of different versions are different variants.
BACKTEST_VERSION = 1
MIN_TRADES = 30
MOMENTUM_DAYS = 20
TREND_DAYS = 200
VOLATILITY_DAYS = 20
VOLATILITY_HISTORY = 252
MIN_VOLATILITY_HISTORY = 126
ALPHA = 0.05
TRADING_DAYS_PER_YEAR = 252
LIST_LIMIT = 200
PROBABILITY_RANK = {"high": 0, "medium": 1, "low": 2}

MODE_LABELS = {BacktestMode.CREATED: "Ab Entstehung", BacktestMode.EVENT_DAY: "Ab Ereignistag (Datenleckage)"}
MIN_PROBABILITY_LABELS = {"low": "alle Stufen", "medium": "ab mittel", "high": "nur hoch"}
KIND_LABELS = {TradeKind.SIGNAL: "Signal", TradeKind.PAPER: "Paper-Trading"}
TRADE_STATUS_LABELS = {
    TradeStatus.MEASURED: "gemessen",
    TradeStatus.GAP: "nicht messbar: Lücke",
    TradeStatus.ENDED: "nicht messbar: nicht mehr gehandelt",
    TradeStatus.OPEN: "offen",
}
TREND_LABELS = {"up": "aufwärts", "down": "abwärts"}
VOLATILITY_LABELS = {"calm": "ruhig", "volatile": "unruhig"}
# The model's own and the baselines', in this order.
STRATEGIES = (("model", "Modell"), ("long", "Immer long"), ("random", "Zufall"), ("momentum", "Momentum"))
BASELINES = STRATEGIES[1:]


class InvalidBacktest(ValueError):
    """A backtest's parameters as the form sent them do not hold; the message says why, in German."""


@dataclass(frozen=True)
class Parameters:
    mode: str
    horizon: int
    min_probability: str
    model: str | None
    prompt_version: int | None
    cost_bps: int
    borrow_bps: int

    @property
    def cost(self) -> float:
        """A trade's costs as a share of its stake: both legs bought and sold, and the short leg borrowed."""
        return 4 * self.cost_bps / 10_000 + self.borrow_bps / 10_000 * self.horizon / TRADING_DAYS_PER_YEAR

    def variant(self, training_until: date | None, validation_until: date | None) -> str:
        """Everything a run tests but its data state: runs of one variant test the same thing."""
        data = asdict(self) | {
            "training_until": _iso(training_until), "validation_until": _iso(validation_until), "code_version": BACKTEST_VERSION,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @property
    def mode_label(self) -> str:
        return MODE_LABELS[self.mode]

    @property
    def min_probability_label(self) -> str:
        return MIN_PROBABILITY_LABELS[self.min_probability]

    @property
    def model_label(self) -> str:
        return f"{self.model}, Prompt {self.prompt_version}" if self.model else "alle Signale, auch des Analysten"

    @property
    def cost_label(self) -> str:
        return f"{self.cost_bps} bp je Seite und Bein, Leihe {self.borrow_bps} bp pro Jahr ({percent_label(-self.cost)} je Trade)"

    @property
    def summary(self) -> str:
        return f"{self.mode_label} · {self.horizon} Handelstage · {self.min_probability_label} · {self.model_label}"


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


def model_key(model: str, prompt_version: int) -> str:
    return f"{model}|{prompt_version}"


def parse(form: Mapping[str, str]) -> Parameters:
    """The parameters the form sent; InvalidBacktest names the first that does not hold."""
    mode = form.get("mode", "")
    if mode not in MODE_LABELS:
        raise InvalidBacktest("Modus: bitte «Ab Entstehung» oder «Ab Ereignistag» wählen.")
    try:
        horizon = int(form.get("horizon", ""))
    except ValueError:
        horizon = 0
    if horizon not in FORECAST_HORIZONS:
        raise InvalidBacktest("Horizont: bitte 5, 20 oder 60 Handelstage wählen.")
    min_probability = form.get("min_probability", "")
    if min_probability not in PROBABILITY_RANK:
        raise InvalidBacktest("Wahrscheinlichkeit: bitte eine Stufe wählen.")
    model, prompt_version = None, None
    if form.get("model"):
        model, _, version = form["model"].rpartition("|")
        if not model or not version.isdigit():
            raise InvalidBacktest("Modell: bitte eines aus der Liste wählen.")
        prompt_version = int(version)
    costs = {}
    for name in ("backtest_cost_bps", "backtest_borrow_bps"):
        try:
            costs[name] = platform_settings_service.FORM_FIELDS[name].parse(form.get(name, ""))
        except InvalidSetting as error:
            raise InvalidBacktest(str(error)) from None
    return Parameters(mode, horizon, min_probability, model, prompt_version, costs["backtest_cost_bps"], costs["backtest_borrow_bps"])


def _payload(parameters: Parameters, as_of: datetime, training: date | None, validation: date | None, reproduces: int | None) -> dict:
    return {
        "parameters": asdict(parameters), "as_of": as_of.isoformat(), "training_until": _iso(training),
        "validation_until": _iso(validation), "reproduces_id": reproduces,
    }


def start(engine: Engine, form: Mapping[str, str], now: datetime | None = None) -> int:
    """Queue a run of the parameters the form sent, its data state now and the periods as set now; the job's id."""
    parameters = parse(form)
    with engine.begin() as connection:
        settings = platform_settings_service.load(connection)
        payload = _payload(parameters, now or datetime.now(UTC), settings.study_training_until, settings.study_validation_until, None)
        return job_service.enqueue(connection, RUN_BACKTEST_JOB_KIND, payload, priority=BACKFILL_PRIORITY)


def reproduce(engine: Engine, run_id: int) -> int | None:
    """Queue the run again with its parameters, data state and periods; the job's id, None for no such run."""
    with engine.begin() as connection:
        row = backtest_repository.run(connection, run_id)
        if row is None:
            return None
        payload = _payload(_parameters(row), row.as_of, row.training_until, row.validation_until, row.id)
        return job_service.enqueue(connection, RUN_BACKTEST_JOB_KIND, payload, priority=BACKFILL_PRIORITY)


def _parameters(row) -> Parameters:
    return Parameters(row.mode, row.horizon, row.min_probability, row.model, row.prompt_version, row.cost_bps, row.borrow_bps)


def run(engine: Engine, payload: Mapping) -> Callable[[Connection], None]:
    """The job "Backtest rechnen": its trades computed outside any transaction, the run stored by the effect."""
    parameters = Parameters(**payload["parameters"])
    as_of = datetime.fromisoformat(payload["as_of"])
    training, validation = _date(payload["training_until"]), _date(payload["validation_until"])
    with engine.connect() as connection:
        trades = compute(connection, parameters, as_of, training, validation)
    values = asdict(parameters) | {
        "as_of": as_of, "code_version": BACKTEST_VERSION, "variant": parameters.variant(training, validation),
        "training_until": training, "validation_until": validation, "trades_hash": trades_hash(trades),
        "reproduces_id": payload.get("reproduces_id"),
    }

    def effect(connection: Connection) -> None:
        run_id = backtest_repository.insert_run(connection, values, trades)
        logger.info("Backtest stored", extra={"run_id": run_id, "trades": len(trades)})

    return effect


# The computation


@dataclass(frozen=True)
class _Signal:
    kind: str
    version_id: int
    forecast_id: int | None
    hypothesis_id: int
    event_id: int
    direction: str
    probability: str
    signal_at: datetime
    target_entity_id: int | None = None
    instrument_id: int | None = None
    benchmark_id: int | None = None


def _passes(parameters: Parameters, probability: str, model: str | None, prompt_version: int | None) -> bool:
    if PROBABILITY_RANK[probability] > PROBABILITY_RANK[parameters.min_probability]:
        return False
    return parameters.model is None or (model, prompt_version) == (parameters.model, parameters.prompt_version)


def signals(rows: Sequence, parameters: Parameters) -> list[_Signal]:
    """The signals among the versions (backtest_repository.signal_versions): a version repeating its hypothesis's last
    signal is none; in the mode "ab Ereignistag" only a hypothesis's first, from its event's first report."""
    last: dict[int, tuple[str, str]] = {}
    found = []
    for row in rows:
        said = (row.direction, row.probability)
        if last.get(row.hypothesis_id) == said:
            continue
        first = row.hypothesis_id not in last
        last[row.hypothesis_id] = said
        if parameters.mode == BacktestMode.EVENT_DAY and not first:
            continue
        if not _passes(parameters, row.probability, row.model, row.prompt_version):
            continue
        signal_at = row.first_reported_at if parameters.mode == BacktestMode.EVENT_DAY else row.created_at
        found.append(_Signal(
            TradeKind.SIGNAL, row.id, None, row.hypothesis_id, row.event_id, row.direction, row.probability, signal_at,
            target_entity_id=row.target_entity_id,
        ))
    return found


@dataclass
class _Prices:
    """A Wertpapier's daily prices as received by the data state."""

    symbol: str
    delisted_on: date | None
    reached: date | None
    opens: dict
    closes: dict
    splits: list
    dividends: dict


def _change(prices: _Prices, days: Sequence[date], first: int, last: int) -> float | None:
    """The return from the close of position `first` to that of `last`; None for a missing close."""
    span = days[first : last + 1]
    if any(day not in prices.closes for day in span):
        return None
    returns = event_study_service.daily_returns(span, prices.closes, prices.splits, prices.dividends)
    return math.prod(1 + returns[index] for index in range(1, len(span))) - 1


def holding_return(prices: _Prices, days: Sequence[date], entry: int, exit_: int) -> tuple[float | None, list[date]]:
    """From the open of position `entry` to the close of `exit_`, and the days missing a price: the entry's open and
    every close."""
    missing = [day for day in days[entry : exit_ + 1] if day not in prices.closes]
    if days[entry] not in missing and prices.opens.get(days[entry]) is None:
        missing.insert(0, days[entry])
    if missing:
        return None, missing
    rest = _change(prices, days, entry, exit_)
    return float(prices.closes[days[entry]] / prices.opens[days[entry]]) * (1 + rest) - 1, []


def _missing_status(prices: _Prices, missing: list[date]) -> TradeStatus:
    last = missing[-1]
    if prices.reached is None or last > prices.reached:
        return TradeStatus.OPEN
    if prices.delisted_on is not None and last > prices.delisted_on:
        return TradeStatus.ENDED
    return TradeStatus.GAP


_SEVERITY = (TradeStatus.OPEN, TradeStatus.ENDED, TradeStatus.GAP, TradeStatus.MEASURED)


def phase(market: _Prices | None, days: Sequence[date], entry: int) -> tuple[str | None, str | None]:
    """The market at the entry, from SPY's closes before it: its trend - the last close over or under the mean of the
    200 before - and its volatility - of the last 20 returns, over or under the median of the year before. None where
    the closes do not reach."""
    last = entry - 1
    if market is None or last < 0:
        return None, None
    trend = None
    window = [market.closes[day] for day in days[max(last - TREND_DAYS + 1, 0) : last + 1] if day in market.closes]
    if len(window) >= TREND_DAYS * 0.9 and days[last] in market.closes:
        trend = "up" if market.closes[days[last]] > sum(window) / len(window) else "down"
    first = max(last - VOLATILITY_HISTORY - VOLATILITY_DAYS, 0)
    span = days[first : last + 1]
    returns = event_study_service.daily_returns(span, market.closes, market.splits, market.dividends)

    def volatility(index: int) -> float | None:
        values = [returns.get(each) for each in range(index - VOLATILITY_DAYS + 1, index + 1)]
        return statistics.stdev(values) if index - VOLATILITY_DAYS + 1 >= 1 and None not in values else None

    current = volatility(len(span) - 1)
    history = [each for each in (volatility(index) for index in range(len(span) - VOLATILITY_HISTORY, len(span))) if each is not None]
    if current is None or len(history) < MIN_VOLATILITY_HISTORY:
        return trend, None
    return trend, "volatile" if current > statistics.median(history) else "calm"


def random_direction(signal: _Signal) -> str:
    """A direction by chance, the same for the same trade in every run."""
    draw = random.Random(f"{signal.kind}:{signal.version_id}:{signal.forecast_id}").random()
    return "positive" if draw < 0.5 else "negative"


def compute(connection: Connection, parameters: Parameters, as_of: datetime, training: date | None, validation: date | None) -> list[dict]:
    """The run's trades: its signals and the logged Prognosen of its horizon, each measured as received by `as_of`."""
    calendar = backtest_repository.trading_days(connection)
    days = [row.day for row in calendar]
    opens_at = [row.opens_at for row in calendar]
    closes_at = [row.closes_at for row in calendar]

    found = signals(backtest_repository.signal_versions(connection, as_of), parameters)
    found += [
        _Signal(TradeKind.PAPER, row.hypothesis_version_id, row.id, row.hypothesis_id, row.event_id, row.direction,
                row.probability, row.created_at, instrument_id=row.instrument_id, benchmark_id=row.benchmark_instrument_id)
        for row in backtest_repository.forecasts(connection, as_of, parameters.horizon)
        if _passes(parameters, row.probability, row.model, row.prompt_version)
    ]
    benchmarks = instrument_repository.by_symbols(connection, PriceSource.ALPACA, BENCHMARK_SYMBOLS)
    market = benchmarks.get(MARKET_BENCHMARK)
    targets = defaultdict(list)
    for row in backtest_repository.instruments_of(connection, {each.target_entity_id for each in found if each.target_entity_id}):
        targets[row.entity_id].append(row)
    sectors = event_study_service.sectors_of(connection, {each.event_id for each in found if each.kind == TradeKind.SIGNAL})

    planned = []  # signal, entry, exit, asset id, benchmark id
    for signal in found:
        entry = bisect_right(opens_at, signal.signal_at)
        entry_day = days[entry] if entry < len(days) else None
        asset_id, benchmark_id = signal.instrument_id, signal.benchmark_id
        if signal.kind == TradeKind.SIGNAL:
            # Traded at the entry, whether or not it is still: no survivorship.
            asset = next(
                (row for row in targets[signal.target_entity_id]
                 if row.delisted_on is None or (entry_day is not None and row.delisted_on >= entry_day)),
                None,
            )
            sector = sectors.get(signal.event_id)
            benchmark = benchmarks.get(sector.symbol) if sector and sector.symbol in benchmarks else market
            if asset is None or benchmark is None or asset.id == benchmark.id:
                continue
            asset_id, benchmark_id = asset.id, benchmark.id
        planned.append((signal, entry, entry + parameters.horizon - 1, asset_id, benchmark_id))

    prices = _prices(connection, planned, market, days, closes_at, as_of)
    trades = []
    for signal, entry, exit_, asset_id, benchmark_id in planned:
        trades.append(_trade(signal, entry, exit_, prices, asset_id, benchmark_id, market, days, closes_at, as_of, training, validation))
    return trades


def _prices(connection: Connection, planned: list, market, days: Sequence[date], closes_at: Sequence[datetime], as_of: datetime) -> dict[int, _Prices]:
    spans: dict[int, list[int]] = {}

    def widen(instrument_id: int, first: int, last: int) -> None:
        first, last = max(first, 0), min(last, len(days) - 1)
        if first > last:
            return
        span = spans.setdefault(instrument_id, [first, last])
        span[0], span[1] = min(span[0], first), max(span[1], last)

    for signal, entry, exit_, asset_id, benchmark_id in planned:
        before = bisect_right(closes_at, signal.signal_at) - 1
        for each in (asset_id, benchmark_id):
            widen(each, min(before - MOMENTUM_DAYS, entry), exit_)
        if market is not None:
            widen(market.id, entry - 1 - VOLATILITY_HISTORY - VOLATILITY_DAYS - 1, entry - 1)
    if not spans:
        return {}
    opens, closes = defaultdict(dict), defaultdict(dict)
    for bar in backtest_repository.daily_bars(connection, [(key, days[a], days[b]) for key, (a, b) in spans.items()], as_of):
        closes[bar.instrument_id][bar.day] = bar.close
        if bar.open is not None:
            opens[bar.instrument_id][bar.day] = bar.open
    splits, dividends = event_study_service.splits_and_dividends(backtest_repository.actions(connection, spans, (*SPLITS, *DIVIDENDS), as_of))
    reached = backtest_repository.reached(connection, spans, as_of)
    rows = backtest_repository.instruments(connection, spans)
    return {
        key: _Prices(rows[key].symbol, rows[key].delisted_on, reached.get(key), opens[key], closes[key], splits[key], dividends[key])
        for key in spans
    }


def _trade(
    signal: _Signal, entry: int, exit_: int, prices: dict[int, _Prices], asset_id: int, benchmark_id: int, market,
    days: Sequence[date], closes_at: Sequence[datetime], as_of: datetime, training: date | None, validation: date | None,
) -> dict:
    entry_day = days[entry] if entry < len(days) else None
    exit_day = days[exit_] if exit_ < len(days) else None
    period = period_of(entry_day or signal.signal_at.astimezone(UTC).date(), training, validation)
    end = period_end(period, training, validation)
    trade = {
        "kind": signal.kind, "hypothesis_version_id": signal.version_id, "forecast_id": signal.forecast_id,
        "hypothesis_id": signal.hypothesis_id, "event_id": signal.event_id, "instrument_id": asset_id,
        "benchmark_instrument_id": benchmark_id, "direction": signal.direction, "probability": signal.probability,
        "signal_at": signal.signal_at, "entry_day": entry_day, "exit_day": exit_day, "period": period,
        "cut": end is not None and (exit_day is None or exit_day > end), "trend": None, "volatility": None,
        "status": TradeStatus.OPEN, "asset_return": None, "benchmark_return": None, "momentum": None,
        "random_direction": random_direction(signal), "missing": [],
    }
    if entry_day is None:
        return trade
    asset, benchmark = prices[asset_id], prices[benchmark_id]
    trade["trend"], trade["volatility"] = phase(prices.get(market.id) if market else None, days, entry)
    before = bisect_right(closes_at, signal.signal_at) - 1
    if before - MOMENTUM_DAYS >= 0:
        moves = [_change(each, days, before - MOMENTUM_DAYS, before) for each in (asset, benchmark)]
        trade["momentum"] = None if None in moves else moves[0] - moves[1]
    if exit_day is None or closes_at[exit_] > as_of:
        return trade
    legs = [(each, *holding_return(each, days, entry, exit_)) for each in (asset, benchmark)]
    if all(value is not None for _, value, _ in legs):
        return trade | {"status": TradeStatus.MEASURED, "asset_return": legs[0][1], "benchmark_return": legs[1][1]}
    statuses = [_missing_status(each, missing) for each, value, missing in legs if value is None]
    trade["status"] = min(statuses, key=_SEVERITY.index)
    trade["missing"] = [{"symbol": each.symbol, "day": day.isoformat()} for each, _, missing in legs for day in missing]
    return trade


def trades_hash(trades: list[dict]) -> str:
    """SHA-256 over the trades as computed: a run again with the same data state compares it."""

    def plain(value):
        if isinstance(value, datetime | date):
            return value.isoformat()
        if isinstance(value, float):
            return round(value, 12)
        if isinstance(value, Decimal):
            return str(value)
        return value

    rows = [{key: plain(value) for key, value in trade.items()} for trade in trades]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


# Figures


def strategy_direction(strategy: str, trade) -> str | None:
    """The direction a strategy takes on a trade; None: it takes none (momentum without its 20 days)."""
    if strategy == "model":
        return trade.direction
    if strategy == "long":
        return "positive"
    if strategy == "random":
        return trade.random_direction
    if trade.momentum is None or trade.momentum == 0:
        return None
    return "positive" if trade.momentum > 0 else "negative"


@dataclass(frozen=True)
class Figures:
    count: int
    hits: int
    mean: float | None
    t: float | None
    total: float
    drawdown: float

    @property
    def hit_label(self) -> str:
        return f"{self.hits} von {self.count} ({share_label(self.hits / self.count)})" if self.count else "–"

    @property
    def mean_label(self) -> str:
        return percent_label(self.mean)

    @property
    def t_label(self) -> str:
        return number_label(self.t)

    @property
    def total_label(self) -> str:
        return percent_label(self.total) if self.count else "–"

    @property
    def drawdown_label(self) -> str:
        return percent_label(-self.drawdown) if self.count else "–"


def figures(trades: Sequence, strategy: str, cost: float) -> Figures:
    """Of the measured trades: how many the strategy took, its hits - the direction right, before costs -, its mean
    net return with the t value, their sum, and the sum's deepest fall from a high, by exit day."""
    nets, hits = [], 0
    for trade in sorted((each for each in trades if each.status == TradeStatus.MEASURED), key=lambda each: (each.exit_day, each.id)):
        direction = strategy_direction(strategy, trade)
        if direction is None:
            continue
        gross = (1 if direction == "positive" else -1) * (trade.asset_return - trade.benchmark_return)
        hits += gross > 0
        nets.append(gross - cost)
    count = len(nets)
    mean = statistics.fmean(nets) if nets else None
    t = None
    if count >= 2:
        spread = statistics.stdev(nets)
        t = mean / (spread / math.sqrt(count)) if spread else None
    level = peak = drawdown = 0.0
    for net in nets:
        level += net
        peak = max(peak, level)
        drawdown = max(drawdown, peak - level)
    return Figures(count, hits, mean, t, sum(nets), drawdown)


def threshold(variants: int) -> float:
    """The |t| a mean must reach in the test period when that many variants were tried on it: Bonferroni at 5 %."""
    return NormalDist().inv_cdf(1 - ALPHA / (2 * max(variants, 1)))


@dataclass(frozen=True)
class Segment:
    key: str
    label: str
    figures: dict[str, Figures]  # by strategy

    @property
    def model(self) -> Figures:
        return self.figures["model"]


def segments(trades: Sequence, cost: float, divided: bool) -> list[Segment]:
    """The figures of every strategy over all signals, per period, per market phase, and of the paper trading."""
    signal = [each for each in trades if each.kind == TradeKind.SIGNAL]
    groups = [("all", "Alle Signale", signal)]
    if divided:
        groups += [
            (period, PERIOD_LABELS[period], [each for each in signal if each.period == period and not each.cut])
            for period in (Period.TRAINING, Period.VALIDATION, Period.TEST)
        ]
    groups += [
        ("up", "Markt aufwärts", [each for each in signal if each.trend == "up"]),
        ("down", "Markt abwärts", [each for each in signal if each.trend == "down"]),
        ("calm", "Markt ruhig", [each for each in signal if each.volatility == "calm"]),
        ("volatile", "Markt unruhig", [each for each in signal if each.volatility == "volatile"]),
        ("paper", "Paper-Trading", [each for each in trades if each.kind == TradeKind.PAPER]),
    ]
    return [Segment(key, label, {name: figures(group, name, cost) for name, _ in STRATEGIES}) for key, label, group in groups]


@dataclass(frozen=True)
class Calibration:
    probability: str
    all: Figures
    test: Figures | None

    @property
    def label(self) -> str:
        return PROBABILITY_LABELS[self.probability]


def calibration(trades: Sequence, cost: float, divided: bool) -> tuple[list[Calibration], bool | None]:
    """The model's hits per probability, over all signals and in the test period, and whether they rise with the
    probability - None with fewer than two probabilities measured."""
    signal = [each for each in trades if each.kind == TradeKind.SIGNAL]
    rows = []
    for probability in PROBABILITY_RANK:
        mine = [each for each in signal if each.probability == probability]
        test = [each for each in mine if each.period == Period.TEST and not each.cut] if divided else None
        rows.append(Calibration(probability, figures(mine, "model", cost), figures(test, "model", cost) if test is not None else None))
    rates = [row.all.hits / row.all.count for row in rows if row.all.count]
    rising = None if len(rates) < 2 else all(higher >= lower for higher, lower in zip(rates, rates[1:]))
    return rows, rising


@dataclass(frozen=True)
class Verdict:
    state: str  # leakage, in_sample, too_few, not_better, better
    text: str
    paper: str
    proven: bool


def verdict(mode: str, divided: bool, by_key: Mapping[str, Segment], variants: int) -> Verdict:
    """Judged on the test period alone, against the baselines after costs and the threshold of the variants tried;
    proven only with enough paper trading as well. Seismo itself never trades."""
    paper = by_key["paper"].model
    paper_ok = paper.count >= MIN_TRADES and paper.mean is not None and paper.mean > 0
    if paper.count < MIN_TRADES:
        paper_text = f"Paper-Trading: {paper.count} gemessene Prognosen, mindestens {MIN_TRADES} nötig."
    elif paper_ok:
        paper_text = f"Paper-Trading: {paper.count} gemessene Prognosen, im Mittel {paper.mean_label} nach Kosten."
    else:
        paper_text = f"Paper-Trading: {paper.count} gemessene Prognosen, im Mittel {paper.mean_label} nach Kosten - kein Gewinn."
    if mode == BacktestMode.EVENT_DAY:
        return Verdict("leakage", "Nicht beurteilbar: Signale ab dem Ereignistag sind nachträglich bewertet (Datenleckage).", paper_text, False)
    if not divided:
        return Verdict(
            "in_sample", "Nur im Sample: keine Test-Periode festgelegt (Plattform-Einstellungen, Event-Study).", paper_text, False
        )
    test = by_key[Period.TEST]
    model = test.model
    if model.count < MIN_TRADES:
        return Verdict("too_few", f"Zu wenige Trades in der Test-Periode: {model.count}, mindestens {MIN_TRADES}.", paper_text, False)
    reasons = []
    if model.mean is None or model.mean <= 0:
        reasons.append(f"im Mittel {model.mean_label} nach Kosten")
    for name, label in BASELINES:
        other = test.figures[name]
        if other.mean is not None and model.mean is not None and other.mean >= model.mean:
            reasons.append(f"nicht besser als «{label}» ({other.mean_label})")
    limit = threshold(variants)
    if model.t is None or model.t < limit:
        reasons.append(f"t = {model.t_label} unter der Schwelle {number_label(limit)} für {variants} Variante{'n' if variants != 1 else ''}")
    if reasons:
        return Verdict("not_better", "Im Test nicht besser: " + "; ".join(reasons) + ".", paper_text, False)
    text = f"Im Test besser als alle Baselines nach Kosten: im Mittel {model.mean_label}, t = {model.t_label} ≥ {number_label(limit)}."
    return Verdict("better", text, paper_text, paper_ok)


# Views


@dataclass(frozen=True)
class TradeView:
    id: int
    kind: str
    hypothesis_id: int
    event_id: int
    event_title: str
    target_name: str
    instrument_id: int
    asset_symbol: str
    benchmark_symbol: str
    direction: str
    probability: str
    signal_at: datetime
    entry_day: date | None
    exit_day: date | None
    period: str
    cut: bool
    trend: str | None
    volatility: str | None
    status: str
    asset_return: float | None
    benchmark_return: float | None
    momentum: float | None
    random_direction: str
    missing: list
    net: float | None = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def says(self) -> str:
        return f"{DIRECTION_LABELS[self.direction]}, {PROBABILITY_LABELS[self.probability]}"

    @property
    def period_label(self) -> str:
        return PERIOD_LABELS[self.period] + (", reicht darüber hinaus" if self.cut else "")

    @property
    def phase_label(self) -> str:
        return f"{TREND_LABELS.get(self.trend, 'Trend unbekannt')}, {VOLATILITY_LABELS.get(self.volatility, 'Volatilität unbekannt')}"

    @property
    def outcome_label(self) -> str:
        if self.status != TradeStatus.MEASURED:
            return TRADE_STATUS_LABELS[self.status]
        return f"{percent_label(self.net)} netto"

    @property
    def returns_label(self) -> str | None:
        if self.status != TradeStatus.MEASURED:
            return None
        return f"{self.asset_symbol} {percent_label(self.asset_return)}, {self.benchmark_symbol} {percent_label(self.benchmark_return)}"

    @property
    def missing_label(self) -> str | None:
        if not self.missing:
            return None
        shown = ", ".join(f"{each['symbol']} {date.fromisoformat(each['day']).strftime('%d.%m.%Y')}" for each in self.missing[:3])
        return f"ohne Kurs: {shown}" + (f" und {len(self.missing) - 3} weitere" if len(self.missing) > 3 else "")


def _trade_view(row, cost: float) -> TradeView:
    names = TradeView.__dataclass_fields__
    values = {name: value for name, value in row._mapping.items() if name in names}
    net = None
    if row.status == TradeStatus.MEASURED:
        net = (1 if row.direction == "positive" else -1) * (row.asset_return - row.benchmark_return) - cost
    return TradeView(**values, net=net)


@dataclass(frozen=True)
class RunView:
    id: int
    as_of: datetime
    created_at: datetime
    code_version: int
    parameters: Parameters
    training_until: date | None
    validation_until: date | None
    trades_hash: str
    reproduces_id: int | None
    reproduced: bool | None  # the same as the run it repeats
    differing: int  # trades that differ from it
    segments: list[Segment]
    calibration: list[Calibration]
    rising: bool | None
    verdict: Verdict
    warnings: list[str]
    variants: int
    threshold: float
    trades: list[TradeView]
    counts: dict[str, int]

    @property
    def periods_label(self) -> str:
        if self.training_until is None:
            return "nicht aufgeteilt"
        text = f"Training bis {self.training_until.strftime('%d.%m.%Y')}"
        if self.validation_until:
            text += f", Validierung bis {self.validation_until.strftime('%d.%m.%Y')}"
        return text + ", danach Test"

    @property
    def threshold_label(self) -> str:
        return number_label(self.threshold)

    @property
    def strategies(self) -> tuple:
        return STRATEGIES

    @property
    def min_trades(self) -> int:
        return MIN_TRADES


def _difference(mine: Sequence, theirs: Sequence) -> int:
    """How many trades differ between two runs, by what they are and what came of them."""

    def keyed(rows):
        return {
            (row.kind, row.hypothesis_version_id, row.forecast_id): (
                row.instrument_id, row.benchmark_instrument_id, row.entry_day, row.exit_day, row.status,
                None if row.asset_return is None else round(row.asset_return, 12),
                None if row.benchmark_return is None else round(row.benchmark_return, 12),
            )
            for row in rows
        }

    a, b = keyed(mine), keyed(theirs)
    return sum(1 for key in a.keys() | b.keys() if a.get(key) != b.get(key))


def detail(engine: Engine, run_id: int) -> RunView | None:
    with engine.connect() as connection:
        row = backtest_repository.run(connection, run_id)
        if row is None:
            return None
        rows = backtest_repository.trades(connection, run_id)
        variants = backtest_repository.variants_on_test(connection)
        period_choices = backtest_repository.period_choices(connection)
        reproduced, differing = None, 0
        if row.reproduces_id is not None:
            original = backtest_repository.run(connection, row.reproduces_id)
            reproduced = original is not None and original.trades_hash == row.trades_hash
            if not reproduced and original is not None:
                differing = _difference(rows, backtest_repository.trades(connection, original.id))
    parameters = _parameters(row)
    cost = parameters.cost
    trades = [_trade_view(each, cost) for each in rows]
    divided = row.training_until is not None
    found = segments(trades, cost, divided)
    by_key = {each.key: each for each in found}
    rows_, rising = calibration(trades, cost, divided)
    judged = verdict(row.mode, divided, by_key, variants)
    warnings = []
    if row.mode == BacktestMode.EVENT_DAY:
        warnings.append(
            "Datenleckage: Die Signale gelten ab der ersten Meldung, wurden aber erst später bewertet - mit Meldungen und "
            "einem Modellwissen, das es am Ereignistag noch nicht gab. Kein Beleg für das Modell."
        )
    if not divided:
        warnings.append("Keine Perioden festgelegt: alles ist im Sample. Training und Validierung in den Plattform-Einstellungen setzen.")
    if variants > 1:
        warnings.append(
            f"Mehrfachtest: Test-Perioden wurden in {variants} Varianten ausgewertet. Je mehr Varianten, desto eher sieht eine "
            f"zufällig gut aus - die Schwelle steigt auf |t| ≥ {number_label(threshold(variants))} (Bonferroni, 5 %)."
        )
    if period_choices > 1:
        warnings.append(
            f"Perioden geändert: Die Läufe nutzten {period_choices} verschiedene Aufteilungen. Wer die Test-Periode nach "
            "gesehenen Ergebnissen verschiebt, macht sie zum Sample."
        )
    counts = {
        "signals": sum(1 for each in trades if each.kind == TradeKind.SIGNAL),
        "measured": sum(1 for each in trades if each.kind == TradeKind.SIGNAL and each.status == TradeStatus.MEASURED),
        "open": sum(1 for each in trades if each.kind == TradeKind.SIGNAL and each.status == TradeStatus.OPEN),
        "unmeasured": sum(1 for each in trades if each.kind == TradeKind.SIGNAL and each.status in (TradeStatus.GAP, TradeStatus.ENDED)),
        "paper": sum(1 for each in trades if each.kind == TradeKind.PAPER),
    }
    return RunView(
        row.id, row.as_of, row.created_at, row.code_version, parameters, row.training_until, row.validation_until,
        row.trades_hash, row.reproduces_id, reproduced, differing, found, rows_, rising, judged, warnings, variants,
        threshold(variants), trades, counts,
    )


@dataclass(frozen=True)
class RunItem:
    id: int
    as_of: datetime
    parameters: Parameters
    reproduces_id: int | None
    signals: int
    measured: int


@dataclass(frozen=True)
class PendingItem:
    job_id: int
    status: str
    as_of: datetime
    parameters: Parameters
    reproduces_id: int | None

    @property
    def status_label(self) -> str:
        return "läuft" if self.status == "running" else "wartet"


@dataclass(frozen=True)
class FormChoices:
    models: list[tuple[str, str]]  # value, label
    cost_bps: int
    borrow_bps: int


@dataclass(frozen=True)
class Overview:
    runs: list[RunItem]
    pending: list[PendingItem]
    choices: FormChoices


def overview(engine: Engine) -> Overview:
    with engine.connect() as connection:
        runs = backtest_repository.runs(connection, LIST_LIMIT)
        pending = backtest_repository.pending(connection, RUN_BACKTEST_JOB_KIND)
        models = backtest_repository.models(connection)
        settings = platform_settings_service.load(connection)
    items = [RunItem(row.id, row.as_of, _parameters(row), row.reproduces_id, row.signals, row.measured) for row in runs]
    waiting = [
        PendingItem(row.id, row.status, datetime.fromisoformat(row.payload["as_of"]), Parameters(**row.payload["parameters"]),
                    row.payload.get("reproduces_id"))
        for row in pending
    ]
    choices = FormChoices(
        [("", "Alle Signale, auch des Analysten")]
        + [(model_key(row.model, row.prompt_version), f"{row.model}, Prompt {row.prompt_version}") for row in models],
        settings.backtest_cost_bps, settings.backtest_borrow_bps,
    )
    return Overview(items, waiting, choices)
