"""Data access for the Kurspunkte (price_point): stored as received, each once."""
from collections.abc import Sequence

from sqlalchemy import Connection, Row, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import PricePoint, PriceResolution


def insert_prices(connection: Connection, rows: list[dict]) -> int:
    """The new ones; a price the source sends again with the same time - the market closed - is stored once."""
    if not rows:
        return 0
    statement = insert(PricePoint).values(rows).on_conflict_do_nothing(
        index_elements=[PricePoint.instrument_id, PricePoint.resolution, PricePoint.at]
    )
    return connection.execute(statement).rowcount


def recent(connection: Connection, instrument_id: int, limit: int, resolution: str = PriceResolution.QUOTE) -> Sequence[Row]:
    """The Wertpapier's latest Kurspunkte of the resolution, the latest first."""
    statement = (
        select(PricePoint.__table__)
        .where(PricePoint.instrument_id == instrument_id, PricePoint.resolution == resolution)
        .order_by(PricePoint.at.desc(), PricePoint.id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def count(connection: Connection, instrument_id: int | None = None, resolution: str | None = None) -> int:
    statement = select(func.count()).select_from(PricePoint)
    if instrument_id is not None:
        statement = statement.where(PricePoint.instrument_id == instrument_id)
    if resolution is not None:
        statement = statement.where(PricePoint.resolution == resolution)
    return connection.execute(statement).scalar_one()


def estimated_count(connection: Connection) -> int:
    """All Kurspunkte as PostgreSQL's statistics estimate them - counting tens of millions takes seconds; counted
    while they have none yet."""
    estimate = connection.execute(text("SELECT reltuples::bigint FROM pg_class WHERE oid = 'price_point'::regclass")).scalar_one()
    return estimate if estimate >= 0 else count(connection)
