"""Data access for the market data behind the daily prices: the trading days, the corporate actions (Kapitalmassnahmen),
how far each sync came, and the daily prices as known at a moment (datamodel.md, rule 14)."""
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, Connection, Date, DateTime, Row, and_, cast, column, func, select, values
from sqlalchemy.dialects.postgresql import insert

from app.db.models import CorporateAction, MarketDataSync, PricePoint, PriceResolution, TradingDay

# A daily price's trading day: its time is the day's start in New York, as Alpaca sends it.
_DAY = cast(func.timezone("America/New_York", PricePoint.at), Date)
_DAILY = PricePoint.resolution == PriceResolution.DAY
_NEW_YORK = ZoneInfo("America/New_York")


def upsert_trading_days(connection: Connection, rows: list[dict]) -> None:
    if not rows:
        return
    statement = insert(TradingDay).values(rows)
    connection.execute(statement.on_conflict_do_update(
        index_elements=[TradingDay.day], set_={"opens_at": statement.excluded.opens_at, "closes_at": statement.excluded.closes_at},
    ))


def last_closed_day(connection: Connection, before: datetime) -> date | None:
    """The latest trading day closed before `before`."""
    return connection.execute(select(func.max(TradingDay.day)).where(TradingDay.closes_at < before)).scalar_one()


def count_trading_days(connection: Connection) -> int:
    return connection.execute(select(func.count()).select_from(TradingDay)).scalar_one()


def sync(connection: Connection, name: str) -> Row | None:
    return connection.execute(select(MarketDataSync.__table__).where(MarketDataSync.name == name)).one_or_none()


def record_sync(connection: Connection, name: str, through: date | None) -> None:
    statement = insert(MarketDataSync).values(name=name, through=through)
    connection.execute(statement.on_conflict_do_update(
        index_elements=[MarketDataSync.name], set_={"through": statement.excluded.through, "synced_at": func.now()},
    ))


def insert_actions(connection: Connection, rows: list[dict]) -> int:
    """The new ones; one the source sends again is kept once, as first received."""
    if not rows:
        return 0
    statement = insert(CorporateAction).values(rows).on_conflict_do_nothing(
        index_elements=[CorporateAction.source, CorporateAction.source_ref, CorporateAction.instrument_id]
    )
    return connection.execute(statement).rowcount


def actions(connection: Connection, instrument_id: int, kinds: Sequence[str] | None = None) -> Sequence[Row]:
    """The Wertpapier's corporate actions, the latest first."""
    statement = select(CorporateAction.__table__).where(CorporateAction.instrument_id == instrument_id)
    if kinds is not None:
        statement = statement.where(CorporateAction.kind.in_(list(kinds)))
    day = func.coalesce(CorporateAction.ex_date, CorporateAction.effective_date, CorporateAction.process_date)
    return connection.execute(statement.order_by(day.desc().nulls_last(), CorporateAction.id.desc())).all()


def actions_of(connection: Connection, instrument_ids: Iterable[int], kinds: Sequence[str]) -> Sequence[Row]:
    """The corporate actions of these kinds of these Wertpapiere, by Wertpapier and day."""
    statement = (
        select(CorporateAction.__table__)
        .where(CorporateAction.instrument_id.in_(list(instrument_ids)), CorporateAction.kind.in_(list(kinds)))
        .order_by(CorporateAction.instrument_id, CorporateAction.ex_date, CorporateAction.id)
    )
    return connection.execute(statement).all()


def trading_days(connection: Connection) -> Sequence[Row]:
    """Every trading day the calendar knows - day, closes_at -, the earliest first: a few thousand."""
    return connection.execute(select(TradingDay.day, TradingDay.closes_at).order_by(TradingDay.day)).all()


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(), _NEW_YORK)


def daily_closes(connection: Connection, spans: Sequence[tuple[int, date, date]]) -> Sequence[Row]:
    """The daily closes of each Wertpapier from the first to the last day of its span - instrument_id, day, close -, by
    Wertpapier and day. By its time, so the unique index on Wertpapier, resolution and time serves it."""
    if not spans:
        return []
    span = values(
        column("instrument_id", BigInteger), column("starts", DateTime(timezone=True)), column("ends", DateTime(timezone=True)),
        name="span",
    ).data([(instrument_id, _day_start(first), _day_start(last + timedelta(days=1))) for instrument_id, first, last in spans])
    statement = (
        select(PricePoint.instrument_id, _DAY.label("day"), PricePoint.close)
        .join(span, and_(PricePoint.instrument_id == span.c.instrument_id, PricePoint.at >= span.c.starts, PricePoint.at < span.c.ends))
        .where(_DAILY)
        .order_by(PricePoint.instrument_id, PricePoint.at)
    )
    return connection.execute(statement).all()


def known_bars(connection: Connection, instrument_id: int, reference: datetime, limit: int) -> Sequence[Row]:
    """The Wertpapier's latest daily prices whose trading day had closed at `reference` - each with its day -, the
    latest first. A day the calendar does not know counts as not closed."""
    statement = (
        select(PricePoint.__table__, _DAY.label("day"))
        .join(TradingDay, TradingDay.day == _DAY)
        .where(PricePoint.instrument_id == instrument_id, _DAILY, TradingDay.closes_at <= reference)
        .order_by(PricePoint.at.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def coverage(connection: Connection, instrument_id: int) -> Row:
    """First and last day of the Wertpapier's daily prices, their number, and the trading days from the first to the
    last."""
    bars = (
        select(func.min(_DAY).label("first_day"), func.max(_DAY).label("last_day"), func.count().label("bars"))
        .where(PricePoint.instrument_id == instrument_id, _DAILY)
        .subquery()
    )
    days = (
        select(func.count())
        .where(TradingDay.day.between(bars.c.first_day, bars.c.last_day))
        .scalar_subquery()
    )
    return connection.execute(select(bars.c.first_day, bars.c.last_day, bars.c.bars, days.label("trading_days"))).one()


def gaps(connection: Connection, instrument_id: int, first_day: date, last_day: date, limit: int) -> list[date]:
    """The trading days from `first_day` to `last_day` without a daily price of the Wertpapier, the latest first."""
    has_bar = (
        select(PricePoint.id)
        .where(PricePoint.instrument_id == instrument_id, _DAILY, _DAY == TradingDay.day)
        .exists()
    )
    statement = (
        select(TradingDay.day)
        .where(TradingDay.day.between(first_day, last_day), ~has_bar)
        .order_by(TradingDay.day.desc())
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())

