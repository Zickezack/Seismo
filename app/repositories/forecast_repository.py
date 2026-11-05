"""Data access for the Prognosen and their Ergebnisse (forecast_service). A forecast is only ever inserted - the
database refuses every change (datamodel.md, rule 12) -, its outcome inserted once, apart from it."""
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, and_, case, func, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.db.models import (
    AliasScheme,
    Entity,
    EntityAlias,
    EntityKind,
    Event,
    Forecast,
    ForecastOutcome,
    ImpactAssessment,
    ImpactHypothesis,
    ImpactHypothesisVersion,
    Instrument,
    TradingDay,
)

_ASSET, _BENCHMARK = aliased(Instrument), aliased(Instrument)


def version(connection: Connection, version_id: int) -> Row | None:
    """A version of a Wirkungsannahme with its hypothesis's event, target and the assessment it came from."""
    statement = (
        select(
            ImpactHypothesisVersion.id, ImpactHypothesisVersion.hypothesis_id, ImpactHypothesisVersion.status,
            ImpactHypothesisVersion.direction, ImpactHypothesisVersion.probability, ImpactHypothesisVersion.classification,
            ImpactHypothesisVersion.origin, ImpactHypothesis.event_id, ImpactHypothesis.target_entity_id,
            ImpactAssessment.model, ImpactAssessment.prompt_version,
        )
        .join(ImpactHypothesis, ImpactHypothesis.id == ImpactHypothesisVersion.hypothesis_id)
        .outerjoin(ImpactAssessment, ImpactAssessment.id == ImpactHypothesisVersion.assessment_id)
        .where(ImpactHypothesisVersion.id == version_id)
    )
    return connection.execute(statement).one_or_none()


def latest_of_hypothesis(connection: Connection, hypothesis_id: int) -> Row | None:
    statement = (
        select(Forecast.direction, Forecast.probability)
        .where(Forecast.hypothesis_id == hypothesis_id)
        .order_by(Forecast.created_at.desc(), Forecast.id.desc())
        .limit(1)
    )
    return connection.execute(statement).one_or_none()


def insert_forecasts(connection: Connection, rows: list[dict]) -> list[int]:
    if not rows:
        return []
    return list(connection.execute(insert(Forecast).returning(Forecast.id), rows).scalars())


def due(connection: Connection, now: datetime, limit: int) -> Sequence[Row]:
    """Forecasts without an outcome whose last day has closed and whose prices are in so far - the Wertpapier's and
    the benchmark's reach it, or the Wertpapier ended before -, the earliest end first."""
    has_outcome = select(ForecastOutcome.id).where(ForecastOutcome.forecast_id == Forecast.id).exists()
    closed = select(TradingDay.day).where(TradingDay.day == Forecast.end_day, TradingDay.closes_at <= now).exists()
    asset_in = or_(_ASSET.history_through >= Forecast.end_day, _ASSET.delisted_on < Forecast.end_day)
    statement = (
        select(
            Forecast.id, Forecast.entry_day, Forecast.end_day, Forecast.horizon, Forecast.direction,
            _ASSET.id.label("asset_id"), _ASSET.symbol.label("asset_symbol"), _ASSET.name.label("asset_name"),
            _ASSET.history_through.label("asset_history_through"), _ASSET.delisted_on.label("asset_delisted_on"),
            _BENCHMARK.id.label("benchmark_id"), _BENCHMARK.symbol.label("benchmark_symbol"), _BENCHMARK.name.label("benchmark_name"),
            _BENCHMARK.history_through.label("benchmark_history_through"), _BENCHMARK.delisted_on.label("benchmark_delisted_on"),
        )
        .join(_ASSET, _ASSET.id == Forecast.instrument_id)
        .join(_BENCHMARK, _BENCHMARK.id == Forecast.benchmark_instrument_id)
        .where(~has_outcome, closed, asset_in, _BENCHMARK.history_through >= Forecast.end_day)
        .order_by(Forecast.end_day, Forecast.id)
        .limit(limit)
    )
    return connection.execute(statement).all()


def insert_outcome(connection: Connection, values: dict) -> bool:
    """Once per forecast: a second worker's is left out."""
    statement = (
        pg_insert(ForecastOutcome).values(values)
        .on_conflict_do_nothing(index_elements=[ForecastOutcome.forecast_id])
        .returning(ForecastOutcome.id)
    )
    # Not rowcount: a single-row insert into a table with an identity key returns it implicitly, and rowcount is -1.
    return connection.execute(statement).scalar_one_or_none() is not None


def _listing():
    target = aliased(Entity)
    return (
        select(
            Forecast.__table__,
            _ASSET.symbol.label("asset_symbol"), _ASSET.name.label("asset_name"),
            _BENCHMARK.symbol.label("benchmark_symbol"),
            target.id.label("target_id"), target.name.label("target_name"),
            Event.title.label("event_title"),
            ForecastOutcome.status.label("outcome_status"), ForecastOutcome.asset_return, ForecastOutcome.benchmark_return,
            ForecastOutcome.excess_return, ForecastOutcome.hit, ForecastOutcome.missing, ForecastOutcome.adjustments,
            ForecastOutcome.recorded_at,
        )
        .join(_ASSET, _ASSET.id == Forecast.instrument_id)
        .join(_BENCHMARK, _BENCHMARK.id == Forecast.benchmark_instrument_id)
        .join(ImpactHypothesis, ImpactHypothesis.id == Forecast.hypothesis_id)
        .join(target, target.id == ImpactHypothesis.target_entity_id)
        .join(Event, Event.id == Forecast.event_id)
        .outerjoin(ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id)
    )


def listing(connection: Connection, resolved: bool | None, limit: int) -> Sequence[Row]:
    """The latest logged first; `resolved` True: with an outcome, False: without, None: all."""
    statement = _listing()
    if resolved is not None:
        statement = statement.where(ForecastOutcome.id.isnot(None) if resolved else ForecastOutcome.id.is_(None))
    return connection.execute(statement.order_by(Forecast.created_at.desc(), Forecast.id.desc()).limit(limit)).all()


def of_hypotheses(connection: Connection, hypothesis_ids: Iterable[int]) -> Sequence[Row]:
    statement = _listing().where(Forecast.hypothesis_id.in_(list(hypothesis_ids)))
    return connection.execute(statement.order_by(Forecast.created_at.desc(), Forecast.horizon, Forecast.id)).all()


def counts(connection: Connection) -> Row:
    """How many forecasts are open, resolved as measured, and resolved without a measure."""
    statement = (
        select(
            func.count().filter(ForecastOutcome.id.is_(None)).label("open"),
            func.count().filter(ForecastOutcome.status == "measured").label("measured"),
            func.count().filter(and_(ForecastOutcome.id.isnot(None), ForecastOutcome.status != "measured")).label("unmeasured"),
        )
        .select_from(Forecast)
        .outerjoin(ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id)
    )
    return connection.execute(statement).one()


def hit_rates(connection: Connection) -> Sequence[Row]:
    """Per horizon and probability, of the measured outcomes: how many, how many hit, the mean excess return signed
    the forecast's way."""
    signed = case((Forecast.direction == "positive", ForecastOutcome.excess_return), else_=-ForecastOutcome.excess_return)
    statement = (
        select(
            Forecast.horizon, Forecast.probability, func.count().label("count"),
            func.count().filter(ForecastOutcome.hit).label("hits"), func.avg(signed).label("mean_signed"),
        )
        .join(ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id)
        .where(ForecastOutcome.status == "measured")
        .group_by(Forecast.horizon, Forecast.probability)
    )
    return connection.execute(statement).all()


def measured(connection: Connection, horizon: int | None) -> Sequence[Row]:
    """The measured outcomes, of one horizon or all, with what may explain them: id, event_id, horizon, probability,
    classification, origin, model, prompt_version, sector_key and its sector's name, created_at, hit, the excess return
    signed the forecast's way, the event's type and the hypothesis's way - the earliest logged first."""
    signed = case((Forecast.direction == "positive", ForecastOutcome.excess_return), else_=-ForecastOutcome.excess_return)
    sector_name = (
        select(Entity.name)
        .join(EntityAlias, EntityAlias.entity_id == Entity.id)
        .where(
            EntityAlias.scheme == AliasScheme.CATALOGUE, EntityAlias.value == Forecast.sector_key, Entity.kind == EntityKind.SECTOR,
        )
        .order_by(Entity.id)
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        select(
            Forecast.id, Forecast.event_id, Forecast.horizon, Forecast.probability, Forecast.classification, Forecast.origin,
            Forecast.model, Forecast.prompt_version, Forecast.sector_key, sector_name.label("sector_name"), Forecast.created_at,
            ForecastOutcome.hit, signed.label("signed"), Event.event_type, ImpactHypothesis.path,
        )
        .join(ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id)
        .join(Event, Event.id == Forecast.event_id)
        .join(ImpactHypothesis, ImpactHypothesis.id == Forecast.hypothesis_id)
        .where(ForecastOutcome.status == "measured")
        .order_by(Forecast.created_at, Forecast.id)
    )
    if horizon is not None:
        statement = statement.where(Forecast.horizon == horizon)
    return connection.execute(statement).all()
