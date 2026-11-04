"""Data access for the Kurspunkte (price_point): stored as received, each once; a tracked Wertpapier's compressed to
hours once old enough, never deleted without a replacement (datamodel.md, rule 14). And the Erfassungslücken."""
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, delete, exists, false, func, literal, literal_column, null, select, text
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg, insert
from sqlalchemy.orm import aliased

from app.db.models import PriceGap, PricePoint, PriceResolution

# Of the two-key advisory lock of the compression (7301 LLM slots ... 7306 Lagen).
COMPRESS_LOCK_NAMESPACE = 7307


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


def counts_by_resolution(connection: Connection, instrument_id: int) -> dict[str, int]:
    statement = select(PricePoint.resolution, func.count()).where(PricePoint.instrument_id == instrument_id).group_by(PricePoint.resolution)
    return dict(connection.execute(statement).all())


def recent_count(connection: Connection, instrument_ids: Iterable[int], resolutions: Iterable[str], since: datetime) -> int:
    """Their Kurspunkte of the resolutions from `since` on: what tracking takes."""
    instrument_ids = list(instrument_ids)
    if not instrument_ids:
        return 0
    statement = select(func.count()).select_from(PricePoint).where(
        PricePoint.instrument_id.in_(instrument_ids), PricePoint.resolution.in_(list(resolutions)), PricePoint.at >= since
    )
    return connection.execute(statement).scalar_one()


# Compression to hours.

def _hour(at):
    """The UTC hour a time lies in, whatever the session's time zone. Literals, not parameters: grouped by, it must be
    the same expression as selected."""
    return func.date_trunc(literal_column("'hour'"), at, literal_column("'UTC'"))


def try_lock_compression(connection: Connection) -> bool:
    """Until the end of the transaction no other one compresses; False when another does."""
    statement = text("SELECT pg_try_advisory_xact_lock(CAST(:namespace AS integer), 0)")
    return connection.execute(statement, {"namespace": COMPRESS_LOCK_NAMESPACE}).scalar_one()


def oldest(connection: Connection, instrument_id: int, resolution: str, after: datetime | None, before: datetime) -> datetime | None:
    """The time of its oldest point of the resolution from `after` on and before `before`."""
    statement = select(func.min(PricePoint.at)).where(
        PricePoint.instrument_id == instrument_id, PricePoint.resolution == resolution, PricePoint.at < before,
        *([PricePoint.at >= after] if after is not None else []),
    )
    return connection.execute(statement).scalar_one()


def compress(connection: Connection, instrument_id: int, resolution: str, start: datetime, end: datetime) -> tuple[int, int]:
    """Its points of the resolution from `start` to before `end` (whole hours) become one Kurspunkt per hour, in one
    statement: the points deleted are the ones the hours are made of, so none goes without its replacement. Of minutes
    open, high, low and close as the minutes have them and the volume their sum; of quotes, whose open, high, low and
    volume are the day's so far, the prices themselves - the volume unknown. Received when the last of them was (rule
    11). An hour that has one already keeps its late points as they are. The points and hours, as numbers."""
    made = aliased(PricePoint)
    done = exists().where(made.instrument_id == PricePoint.instrument_id, made.resolution == PriceResolution.HOUR, made.at == _hour(PricePoint.at))
    moved = (
        delete(PricePoint)
        .where(PricePoint.instrument_id == instrument_id, PricePoint.resolution == resolution, PricePoint.at >= start,
               PricePoint.at < end, ~done)
        .returning(PricePoint.at, PricePoint.open, PricePoint.high, PricePoint.low, PricePoint.close, PricePoint.volume,
                   PricePoint.source, PricePoint.delayed, PricePoint.received_at)
        .cte("moved")
    )
    hour = _hour(moved.c.at)
    first_close = array_agg(aggregate_order_by(moved.c.close, moved.c.at))[1]
    if resolution == PriceResolution.MINUTE:
        opening = array_agg(aggregate_order_by(moved.c.open, moved.c.at))[1]
        high, low, volume = func.max(moved.c.high), func.min(moved.c.low), func.sum(moved.c.volume)
    else:
        opening, high, low, volume = first_close, func.max(moved.c.close), func.min(moved.c.close), null()
    hours = select(
        literal(instrument_id), literal(PriceResolution.HOUR.value), hour, opening, high, low,
        array_agg(aggregate_order_by(moved.c.close, moved.c.at.desc()))[1], volume, func.max(moved.c.source),
        func.coalesce(func.bool_or(moved.c.delayed), false()), func.max(moved.c.received_at), func.count(),
    ).group_by(hour)
    columns = ["instrument_id", "resolution", "at", "open", "high", "low", "close", "volume", "source", "delayed", "received_at", "samples"]
    made_hours = connection.execute(insert(PricePoint).from_select(columns, hours).returning(PricePoint.samples)).scalars().all()
    return sum(made_hours), len(made_hours)


# Erfassungslücken.

def insert_gap(connection: Connection, instrument_id: int, starts_at: datetime, ends_at: datetime, cause: str) -> None:
    values = {"instrument_id": instrument_id, "starts_at": starts_at, "ends_at": ends_at, "cause": cause}
    connection.execute(insert(PriceGap).values(values))


def gaps(connection: Connection, instrument_id: int, limit: int) -> Sequence[Row]:
    """Its latest gaps, the latest first."""
    statement = (
        select(PriceGap.__table__).where(PriceGap.instrument_id == instrument_id)
        .order_by(PriceGap.starts_at.desc(), PriceGap.id.desc()).limit(limit)
    )
    return connection.execute(statement).all()


def count_gaps(connection: Connection, instrument_id: int) -> int:
    return connection.execute(select(func.count()).select_from(PriceGap).where(PriceGap.instrument_id == instrument_id)).scalar_one()
