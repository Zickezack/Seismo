"""Data access for the Wertpapiere (instrument), each with its latest quote and the span of its daily prices."""
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from sqlalchemy import Connection, Row, and_, delete, exists, false, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.db.models import Entity, Instrument, InstrumentStatus, PricePoint, PriceResolution
from app.repositories.source_repository import NAME_COLLATION

# When it was asked for last, successfully or not: what tells whether its live price is due again.
_ASKED = func.greatest(Instrument.last_fetched_at, Instrument.last_error_at)


def _latest():
    """Per Wertpapier its latest quote."""
    return (
        select(PricePoint)
        .where(PricePoint.resolution == PriceResolution.QUOTE)
        .ext(distinct_on(PricePoint.instrument_id))
        .order_by(PricePoint.instrument_id, PricePoint.at.desc(), PricePoint.id.desc())
        .subquery()
    )


def _listed():
    latest = _latest()
    return (
        select(
            Instrument.__table__,
            Entity.name.label("entity_name"),
            latest.c.at.label("price_at"),
            latest.c.close.label("price"),
            latest.c.previous_close,
            latest.c.source.label("price_source"),
            latest.c.delayed,
            latest.c.received_at,
        )
        .outerjoin(Entity, Entity.id == Instrument.entity_id)
        .outerjoin(latest, latest.c.instrument_id == Instrument.id)
    )


def _filtered(statement, live_only: bool, source: str | None, search: str | None):
    if live_only:
        statement = statement.where(Instrument.live)
    if source:
        statement = statement.where(Instrument.source == source)
    if search:
        lowered = search.lower()
        statement = statement.where(or_(
            func.lower(Instrument.symbol) == lowered, func.strpos(func.lower(Instrument.name), lowered) > 0,
        ))
    return statement


def listing(connection: Connection, live_only: bool, source: str | None, search: str | None, limit: int) -> Sequence[Row]:
    """The Wertpapiere by name - those with a live price only, of one source, with the search in the symbol or the
    name, the one with the symbol searched for first -, at most `limit`."""
    statement = _filtered(_listed(), live_only, source, search)
    if search:
        statement = statement.order_by((func.lower(Instrument.symbol) == search.lower()).desc())
    statement = statement.order_by(Instrument.name.collate(NAME_COLLATION), Instrument.id).limit(limit)
    return connection.execute(statement).all()


def count_listing(connection: Connection, live_only: bool, source: str | None, search: str | None) -> int:
    statement = _filtered(select(func.count()).select_from(Instrument), live_only, source, search)
    return connection.execute(statement).scalar_one()


def get(connection: Connection, instrument_id: int) -> Row | None:
    return connection.execute(_listed().where(Instrument.id == instrument_id)).one_or_none()


def of_entities(connection: Connection, entity_ids: Iterable[int]) -> Sequence[Row]:
    """The Wertpapiere of these entities, traded ones, with their latest price, by name."""
    statement = (
        _listed()
        .where(Instrument.entity_id.in_(list(entity_ids)), Instrument.status == InstrumentStatus.ACTIVE)
        .order_by(Instrument.name.collate(NAME_COLLATION), Instrument.id)
    )
    return connection.execute(statement).all()


def find(connection: Connection, source: str, source_ref: str) -> int | None:
    statement = select(Instrument.id).where(Instrument.source == source, Instrument.source_ref == source_ref)
    return connection.execute(statement).scalar_one_or_none()


def by_symbol(connection: Connection, source: str, symbol: str) -> Row | None:
    """The source's Wertpapier with this symbol now, a traded one before one no longer traded."""
    statement = (
        select(Instrument.__table__)
        .where(Instrument.source == source, Instrument.symbol == symbol)
        .order_by(Instrument.status, Instrument.id)
        .limit(1)
    )
    return connection.execute(statement).one_or_none()


def insert_instrument(connection: Connection, values: dict) -> int:
    return connection.execute(insert(Instrument).values(values).returning(Instrument.id)).scalar_one()


def update_instrument(connection: Connection, instrument_id: int, values: dict) -> bool:
    return connection.execute(update(Instrument).where(Instrument.id == instrument_id).values(values)).rowcount == 1


def upsert_listed(connection: Connection, rows: list[dict]) -> None:
    """The source's list: a new one comes, a known one takes its symbol, exchange, name and status as listed now;
    whether it has a live price, its kind and its entity stay as they are."""
    if not rows:
        return
    statement = pg_insert(Instrument).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Instrument.source, Instrument.source_ref],
        set_={key: statement.excluded[key] for key in ("symbol", "exchange", "name", "status", "history")}
        # Alpaca's list sends no CUSIP: one learned from the corporate actions stays.
        | {key: func.coalesce(statement.excluded[key], getattr(Instrument, key)) for key in ("cusip", "isin")},
    )
    connection.execute(statement)


def symbols(connection: Connection, source: str) -> dict[str, int]:
    """The source's Wertpapiere by their symbol now; of two with one symbol - one no longer traded - the traded one."""
    statement = select(Instrument.symbol, Instrument.id, Instrument.status).where(Instrument.source == source).order_by(
        Instrument.status.desc(), Instrument.id
    )
    return {row.symbol: row.id for row in connection.execute(statement)}


def live_due(connection: Connection, source: str, asked_before: datetime) -> Sequence[Row]:
    """The source's Wertpapiere with a live price not asked for since `asked_before`, by id."""
    statement = (
        select(Instrument.id, Instrument.symbol, Instrument.source_ref)
        .where(Instrument.source == source, Instrument.live, or_(_ASKED.is_(None), _ASKED < asked_before))
        .order_by(Instrument.id)
    )
    return connection.execute(statement).all()


def history_due(connection: Connection, source: str, through: date) -> Sequence[Row]:
    """The source's Wertpapiere whose daily prices do not reach `through` yet, those not fetched at all first, then by
    how far they reach - so a batch shares one start. One no longer traded is fetched once; one whose symbol a traded
    one carries now only once its last day is known - before, its prices could not be told from the other's."""
    other = aliased(Instrument)
    symbol_taken = exists().where(
        other.source == Instrument.source, other.symbol == Instrument.symbol, other.status == InstrumentStatus.ACTIVE,
        other.id != Instrument.id,
    )
    inactive = Instrument.status == InstrumentStatus.INACTIVE
    statement = (
        select(Instrument.id, Instrument.symbol, Instrument.status, Instrument.delisted_on, Instrument.history_through)
        .where(
            Instrument.source == source, Instrument.history,
            or_(Instrument.history_through.is_(None), and_(~inactive, Instrument.history_through < through)),
            ~and_(inactive, Instrument.delisted_on.is_(None), symbol_taken),
        )
        .order_by(Instrument.history_through.asc().nulls_first(), Instrument.status, Instrument.id)
    )
    return connection.execute(statement).all()


def count_history(connection: Connection, source: str, through: date | None) -> Row:
    """Of the source's Wertpapiere with daily prices: how many, and how many are done - reach `through`, or are no
    longer traded and fetched once."""
    reached = Instrument.history_through >= through if through else false()
    fetched_once = and_(Instrument.status == InstrumentStatus.INACTIVE, Instrument.history_through.is_not(None))
    statement = select(func.count().label("total"), func.count().filter(or_(reached, fetched_once)).label("done")).where(
        Instrument.source == source, Instrument.history
    )
    return connection.execute(statement).one()


def forget_history(connection: Connection, source: str) -> None:
    """Fetch every daily price again from the start: the period reaches further back now."""
    connection.execute(update(Instrument).where(Instrument.source == source).values(history_through=None))


def set_history_through(connection: Connection, instrument_ids: list[int], through: date) -> None:
    connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(history_through=through))


def by_ids(connection: Connection, instrument_ids: Iterable[int]) -> Sequence[Row]:
    statement = select(Instrument.__table__).where(Instrument.id.in_(list(instrument_ids))).order_by(Instrument.id)
    return connection.execute(statement).all()


def cusips(connection: Connection, source: str) -> dict[str, int]:
    statement = select(Instrument.cusip, Instrument.id).where(Instrument.source == source, Instrument.cusip.is_not(None))
    return {row.cusip: row.id for row in connection.execute(statement)}


def learn_cusip(connection: Connection, instrument_id: int, cusip: str, isin: str | None) -> None:
    """Its CUSIP, and the ISIN of it where it has none; one it has stays."""
    connection.execute(
        update(Instrument)
        .where(Instrument.id == instrument_id, Instrument.cusip.is_(None))
        .values(cusip=cusip, isin=func.coalesce(Instrument.isin, isin))
    )


def unlinked(connection: Connection, source: str) -> Sequence[Row]:
    """The source's Wertpapiere without an entity."""
    statement = select(
        Instrument.id, Instrument.symbol, Instrument.exchange, Instrument.name, Instrument.isin, Instrument.status,
    ).where(Instrument.source == source, Instrument.entity_id.is_(None))
    return connection.execute(statement).all()


def link_entity(connection: Connection, instrument_id: int, entity_id: int, uncertain: bool) -> None:
    """Its entity, unless it has one: a link made never gives way to another."""
    connection.execute(
        update(Instrument)
        .where(Instrument.id == instrument_id, Instrument.entity_id.is_(None))
        .values(entity_id=entity_id, entity_uncertain=uncertain)
    )


def mark_delisted(connection: Connection, instrument_ids: list[int], day: date) -> None:
    """No longer traded since `day`; an earlier day kept."""
    connection.execute(
        update(Instrument)
        .where(Instrument.id.in_(instrument_ids), or_(Instrument.delisted_on.is_(None), Instrument.delisted_on > day))
        .values(delisted_on=day)
    )


def lock_instruments(connection: Connection, instrument_ids: list[int]) -> Sequence[Row]:
    """Id, name, source and whether it has Kurspunkte, of the existing ones, locked until the end of the transaction."""
    has_prices = exists().where(PricePoint.instrument_id == Instrument.id)
    statement = (
        select(Instrument.id, Instrument.name, Instrument.source, has_prices.label("has_prices"))
        .where(Instrument.id.in_(instrument_ids))
        .order_by(Instrument.id)
        .with_for_update(of=Instrument)
    )
    return connection.execute(statement).all()


def set_live(connection: Connection, instrument_ids: list[int], live: bool) -> int:
    return connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(live=live)).rowcount


def delete_instruments(connection: Connection, instrument_ids: list[int]) -> int:
    return connection.execute(delete(Instrument).where(Instrument.id.in_(instrument_ids))).rowcount


def record_fetched(connection: Connection, instrument_ids: list[int]) -> None:
    connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(last_fetched_at=func.now()))


def record_error(connection: Connection, instrument_ids: list[int], message: str) -> None:
    """The fetch's error for them; the time of the last success stays."""
    values = {"last_error": message[:2000], "last_error_at": func.now()}
    connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(values))
