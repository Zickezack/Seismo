"""Data access for the backtests (backtest_service): the signals and Prognosen as known at a data state, the daily
prices and corporate actions received by then, and the runs with their trades - inserted only, the database refuses
every change."""
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, Connection, Date, DateTime, Row, and_, cast, column, func, insert, select, values
from sqlalchemy.orm import aliased

from app.db.models import (
    BacktestRun,
    BacktestTrade,
    CorporateAction,
    Entity,
    Event,
    Forecast,
    ImpactAssessment,
    ImpactHypothesis,
    ImpactHypothesisVersion,
    ImpactStatus,
    Instrument,
    Job,
    JobStatus,
    PricePoint,
    PriceResolution,
    PriceSource,
    TradingDay,
)

_NEW_YORK = ZoneInfo("America/New_York")
# A daily price's trading day: its time is the day's start in New York, as Alpaca sends it.
_DAY = cast(func.timezone("America/New_York", PricePoint.at), Date)
_DAILY = PricePoint.resolution == PriceResolution.DAY


def trading_days(connection: Connection) -> Sequence[Row]:
    """Every trading day - day, opens_at, closes_at -, the earliest first."""
    return connection.execute(select(TradingDay.day, TradingDay.opens_at, TradingDay.closes_at).order_by(TradingDay.day)).all()


def signal_versions(connection: Connection, as_of: datetime) -> Sequence[Row]:
    """Every version created by `as_of` that says a direction with a probability - proposed or confirmed, positive or
    negative -, with its hypothesis's event and target, the event's first report and the assessment's model; by
    hypothesis, then as created."""
    statement = (
        select(
            ImpactHypothesisVersion.id, ImpactHypothesisVersion.hypothesis_id, ImpactHypothesisVersion.direction,
            ImpactHypothesisVersion.probability, ImpactHypothesisVersion.created_at, ImpactHypothesis.event_id,
            ImpactHypothesis.target_entity_id, Event.first_reported_at, ImpactAssessment.model, ImpactAssessment.prompt_version,
        )
        .join(ImpactHypothesis, ImpactHypothesis.id == ImpactHypothesisVersion.hypothesis_id)
        .join(Event, Event.id == ImpactHypothesis.event_id)
        .outerjoin(ImpactAssessment, ImpactAssessment.id == ImpactHypothesisVersion.assessment_id)
        .where(
            ImpactHypothesisVersion.created_at <= as_of,
            ImpactHypothesisVersion.status.in_((ImpactStatus.PROPOSED, ImpactStatus.CONFIRMED)),
            ImpactHypothesisVersion.probability.isnot(None),
            ImpactHypothesisVersion.direction.in_(("positive", "negative")),
        )
        .order_by(ImpactHypothesisVersion.hypothesis_id, ImpactHypothesisVersion.created_at, ImpactHypothesisVersion.id)
    )
    return connection.execute(statement).all()


def forecasts(connection: Connection, as_of: datetime, horizon: int) -> Sequence[Row]:
    """The Prognosen of this horizon logged by `as_of`, as logged."""
    statement = (
        select(
            Forecast.id, Forecast.hypothesis_version_id, Forecast.hypothesis_id, Forecast.event_id, Forecast.instrument_id,
            Forecast.benchmark_instrument_id, Forecast.direction, Forecast.probability, Forecast.model, Forecast.prompt_version,
            Forecast.created_at,
        )
        .where(Forecast.created_at <= as_of, Forecast.horizon == horizon)
        .order_by(Forecast.created_at, Forecast.id)
    )
    return connection.execute(statement).all()


def instruments_of(connection: Connection, entity_ids: Iterable[int]) -> Sequence[Row]:
    """The Wertpapiere with daily prices (Alpaca's) of these entities, traded or no longer; by entity, a traded one
    first."""
    statement = (
        select(Instrument.id, Instrument.entity_id, Instrument.symbol, Instrument.name, Instrument.delisted_on)
        .where(Instrument.entity_id.in_(list(entity_ids)), Instrument.source == PriceSource.ALPACA, Instrument.history)
        .order_by(Instrument.entity_id, Instrument.status, Instrument.symbol, Instrument.id)
    )
    return connection.execute(statement).all()


def instruments(connection: Connection, ids: Iterable[int]) -> dict[int, Row]:
    statement = select(Instrument.id, Instrument.symbol, Instrument.name, Instrument.delisted_on).where(Instrument.id.in_(list(ids)))
    return {row.id: row for row in connection.execute(statement)}


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(), _NEW_YORK)


def daily_bars(connection: Connection, spans: Sequence[tuple[int, date, date]], as_of: datetime) -> Sequence[Row]:
    """The daily prices received by `as_of` of each Wertpapier from the first to the last day of its span -
    instrument_id, day, open, close -, by Wertpapier and day."""
    if not spans:
        return []
    span = values(
        column("instrument_id", BigInteger), column("starts", DateTime(timezone=True)), column("ends", DateTime(timezone=True)),
        name="span",
    ).data([(instrument_id, _day_start(first), _day_start(last + timedelta(days=1))) for instrument_id, first, last in spans])
    statement = (
        select(PricePoint.instrument_id, _DAY.label("day"), PricePoint.open, PricePoint.close)
        .join(span, and_(PricePoint.instrument_id == span.c.instrument_id, PricePoint.at >= span.c.starts, PricePoint.at < span.c.ends))
        .where(_DAILY, PricePoint.received_at <= as_of)
        .order_by(PricePoint.instrument_id, PricePoint.at)
    )
    return connection.execute(statement).all()


def reached(connection: Connection, instrument_ids: Iterable[int], as_of: datetime) -> dict[int, date]:
    """How far each Wertpapier's daily prices reached at `as_of`: the day of its latest one received by then."""
    statement = (
        select(PricePoint.instrument_id, func.max(PricePoint.at).label("at"))
        .where(PricePoint.instrument_id.in_(list(instrument_ids)), _DAILY, PricePoint.received_at <= as_of)
        .group_by(PricePoint.instrument_id)
    )
    return {row.instrument_id: row.at.astimezone(_NEW_YORK).date() for row in connection.execute(statement)}


def actions(connection: Connection, instrument_ids: Iterable[int], kinds: Sequence[str], as_of: datetime) -> Sequence[Row]:
    """The corporate actions of these kinds of these Wertpapiere received by `as_of`, by Wertpapier and day."""
    statement = (
        select(CorporateAction.__table__)
        .where(
            CorporateAction.instrument_id.in_(list(instrument_ids)), CorporateAction.kind.in_(list(kinds)),
            CorporateAction.received_at <= as_of,
        )
        .order_by(CorporateAction.instrument_id, CorporateAction.ex_date, CorporateAction.id)
    )
    return connection.execute(statement).all()


def models(connection: Connection) -> Sequence[Row]:
    """The LLM's models and prompt versions that assessed effects, the latest first."""
    statement = (
        select(ImpactAssessment.model, ImpactAssessment.prompt_version)
        .group_by(ImpactAssessment.model, ImpactAssessment.prompt_version)
        .order_by(func.max(ImpactAssessment.created_at).desc())
    )
    return connection.execute(statement).all()


def insert_run(connection: Connection, values_: dict, trades: list[dict]) -> int:
    run_id = connection.execute(insert(BacktestRun).values(values_).returning(BacktestRun.id)).scalar_one()
    if trades:
        connection.execute(insert(BacktestTrade), [each | {"run_id": run_id} for each in trades])
    return run_id


def run(connection: Connection, run_id: int) -> Row | None:
    return connection.execute(select(BacktestRun).where(BacktestRun.id == run_id)).one_or_none()


def runs(connection: Connection, limit: int) -> Sequence[Row]:
    """The latest runs first, each with its signal trades and how many were measured."""
    trades = (
        select(
            BacktestTrade.run_id,
            func.count().filter(BacktestTrade.kind == "signal").label("signals"),
            func.count().filter(BacktestTrade.kind == "signal", BacktestTrade.status == "measured").label("measured"),
        )
        .group_by(BacktestTrade.run_id)
        .subquery()
    )
    statement = (
        select(BacktestRun, func.coalesce(trades.c.signals, 0).label("signals"), func.coalesce(trades.c.measured, 0).label("measured"))
        .outerjoin(trades, trades.c.run_id == BacktestRun.id)
        .order_by(BacktestRun.id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def trades(connection: Connection, run_id: int) -> Sequence[Row]:
    """A run's trades with their Wertpapier's and benchmark's symbols, event and target; as stored."""
    asset, benchmark, target = aliased(Instrument), aliased(Instrument), aliased(Entity)
    statement = (
        select(
            BacktestTrade.__table__, asset.symbol.label("asset_symbol"), benchmark.symbol.label("benchmark_symbol"),
            Event.title.label("event_title"), target.name.label("target_name"),
        )
        .join(asset, asset.id == BacktestTrade.instrument_id)
        .join(benchmark, benchmark.id == BacktestTrade.benchmark_instrument_id)
        .join(Event, Event.id == BacktestTrade.event_id)
        .join(ImpactHypothesis, ImpactHypothesis.id == BacktestTrade.hypothesis_id)
        .join(target, target.id == ImpactHypothesis.target_entity_id)
        .where(BacktestTrade.run_id == run_id)
        .order_by(BacktestTrade.id)
    )
    return connection.execute(statement).all()


def variants_on_test(connection: Connection) -> int:
    """How many variants looked at a test period: the distinct fingerprints of the runs with periods."""
    statement = select(func.count(func.distinct(BacktestRun.variant))).where(BacktestRun.training_until.isnot(None))
    return connection.execute(statement).scalar_one()


def period_choices(connection: Connection) -> int:
    """How many different periods the runs used."""
    pairs = select(BacktestRun.training_until, BacktestRun.validation_until).distinct().subquery()
    return connection.execute(select(func.count()).select_from(pairs)).scalar_one()


def pending(connection: Connection, kind: str) -> Sequence[Row]:
    """The runs asked for and not stored yet: their jobs still waiting or running, the earliest first."""
    statement = (
        select(Job.id, Job.status, Job.payload, Job.created_at)
        .where(Job.kind == kind, Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
        .order_by(Job.id)
    )
    return connection.execute(statement).all()
