"""Data access for the Wertpapiere (instrument), each with its latest Kurspunkt."""
from collections.abc import Iterable, Sequence

from sqlalchemy import Connection, Row, delete, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on

from app.db.models import Entity, Instrument, PricePoint, PriceResolution
from app.repositories.source_repository import NAME_COLLATION


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


def listing(connection: Connection) -> Sequence[Row]:
    """Every Wertpapier by name, with its entity and latest price."""
    statement = _listed().order_by(Instrument.name.collate(NAME_COLLATION), Instrument.id)
    return connection.execute(statement).all()


def get(connection: Connection, instrument_id: int) -> Row | None:
    return connection.execute(_listed().where(Instrument.id == instrument_id)).one_or_none()


def of_entities(connection: Connection, entity_ids: Iterable[int]) -> Sequence[Row]:
    """The Wertpapiere of these entities, active ones, with their latest price, by name."""
    statement = (
        _listed()
        .where(Instrument.entity_id.in_(list(entity_ids)), Instrument.is_active)
        .order_by(Instrument.name.collate(NAME_COLLATION), Instrument.id)
    )
    return connection.execute(statement).all()


def find(connection: Connection, symbol: str, exchange: str) -> int | None:
    statement = select(Instrument.id).where(Instrument.symbol == symbol, Instrument.exchange == exchange)
    return connection.execute(statement).scalar_one_or_none()


def insert_instrument(connection: Connection, values: dict) -> int:
    return connection.execute(insert(Instrument).values(values).returning(Instrument.id)).scalar_one()


def update_instrument(connection: Connection, instrument_id: int, values: dict) -> bool:
    return connection.execute(update(Instrument).where(Instrument.id == instrument_id).values(values)).rowcount == 1


def active(connection: Connection) -> Sequence[Row]:
    """The active Wertpapiere, by id: what the worker fetches."""
    statement = select(Instrument.id, Instrument.symbol, Instrument.exchange).where(Instrument.is_active).order_by(Instrument.id)
    return connection.execute(statement).all()


def by_ids(connection: Connection, instrument_ids: Iterable[int]) -> Sequence[Row]:
    statement = select(Instrument.__table__).where(Instrument.id.in_(list(instrument_ids))).order_by(Instrument.id)
    return connection.execute(statement).all()


def lock_instruments(connection: Connection, instrument_ids: list[int]) -> Sequence[Row]:
    """Id, name and whether it has Kurspunkte, of the existing ones, locked until the end of the transaction."""
    has_prices = exists().where(PricePoint.instrument_id == Instrument.id)
    statement = (
        select(Instrument.id, Instrument.name, has_prices.label("has_prices"))
        .where(Instrument.id.in_(instrument_ids))
        .order_by(Instrument.id)
        .with_for_update(of=Instrument)
    )
    return connection.execute(statement).all()


def set_active(connection: Connection, instrument_ids: list[int], active_: bool) -> int:
    return connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(is_active=active_)).rowcount


def delete_instruments(connection: Connection, instrument_ids: list[int]) -> int:
    return connection.execute(delete(Instrument).where(Instrument.id.in_(instrument_ids))).rowcount


def record_fetched(connection: Connection, instrument_ids: list[int]) -> None:
    connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(last_fetched_at=func.now()))


def record_error(connection: Connection, instrument_ids: list[int], message: str) -> None:
    """The fetch's error for them; the time of the last success stays."""
    values = {"last_error": message[:2000], "last_error_at": func.now()}
    connection.execute(update(Instrument).where(Instrument.id.in_(instrument_ids)).values(values))
