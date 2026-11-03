"""Data access for the Watchlisten and what they follow."""
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import Connection, Row, delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.db.models import AlertRule, Entity, Event, Instrument, Situation, WatchItemKind, Watchlist, WatchlistItem
from app.repositories.source_repository import NAME_COLLATION

UNIQUE_NAME_INDEX = "uq_watchlist_name"


class NameTaken(Exception):
    """Another watchlist already has this name, in any case."""


def insert_watchlist(connection: Connection, name: str) -> int:
    with _name_must_be_free(connection):
        return connection.execute(insert(Watchlist).values(name=name).returning(Watchlist.id)).scalar_one()


def rename(connection: Connection, watchlist_id: int, name: str) -> bool:
    """False when the watchlist does not exist."""
    statement = update(Watchlist).where(Watchlist.id == watchlist_id).values(name=name, updated_at=func.now())
    with _name_must_be_free(connection):
        return connection.execute(statement).rowcount == 1


def get(connection: Connection, watchlist_id: int) -> Row | None:
    return connection.execute(select(Watchlist.__table__).where(Watchlist.id == watchlist_id)).one_or_none()


def listing(connection: Connection, watchlist_id: int | None = None) -> Sequence[Row]:
    """Every watchlist, or the one, by name, with the number of its items and of the alert rules that use it."""
    items = select(func.count()).where(WatchlistItem.watchlist_id == Watchlist.id).scalar_subquery()
    rules = select(func.count()).where(AlertRule.watchlist_id == Watchlist.id).scalar_subquery()
    statement = select(Watchlist.__table__, items.label("item_count"), rules.label("rule_count")).order_by(
        Watchlist.name.collate(NAME_COLLATION), Watchlist.id
    )
    if watchlist_id is not None:
        statement = statement.where(Watchlist.id == watchlist_id)
    return connection.execute(statement).all()


def lock_watchlists(connection: Connection, watchlist_ids: list[int]) -> Sequence[Row]:
    """Id and name of the existing ones, locked until the end of the transaction."""
    statement = select(Watchlist.id, Watchlist.name).where(Watchlist.id.in_(watchlist_ids)).order_by(Watchlist.id).with_for_update()
    return connection.execute(statement).all()


def rules_using(connection: Connection, watchlist_ids: list[int]) -> Sequence[Row]:
    statement = select(AlertRule.watchlist_id, AlertRule.name).where(AlertRule.watchlist_id.in_(watchlist_ids)).order_by(
        AlertRule.name.collate(NAME_COLLATION)
    )
    return connection.execute(statement).all()


def delete_watchlists(connection: Connection, watchlist_ids: list[int]) -> int:
    return connection.execute(delete(Watchlist).where(Watchlist.id.in_(watchlist_ids))).rowcount


def add_item(connection: Connection, watchlist_id: int, kind: str, target: int | str) -> bool:
    """False when the watchlist follows it already."""
    column = {
        WatchItemKind.ENTITY: "entity_id", WatchItemKind.EVENT: "event_id", WatchItemKind.INSTRUMENT: "instrument_id",
        WatchItemKind.SITUATION: "situation_id",
    }.get(kind, "value")
    # RETURNING, not rowcount: psycopg reports -1 for an INSERT ... ON CONFLICT here.
    statement = (
        pg_insert(WatchlistItem).values(watchlist_id=watchlist_id, kind=kind, **{column: target})
        .on_conflict_do_nothing().returning(WatchlistItem.id)
    )
    added = connection.execute(statement).first() is not None
    if added:
        connection.execute(update(Watchlist).where(Watchlist.id == watchlist_id).values(updated_at=func.now()))
    return added


def items(connection: Connection, watchlist_id: int) -> Sequence[Row]:
    """What the watchlist follows, each with the name of its entity, event, Wertpapier or Lage; an entity, event or Lage
    merged since with the one it went into, a Wertpapier with the entity it is linked to."""
    own_entity = Entity.__table__.alias("own_entity")
    into_entity = Entity.__table__.alias("into_entity")
    linked_entity = Entity.__table__.alias("linked_entity")
    own_event = Event.__table__.alias("own_event")
    into_event = Event.__table__.alias("into_event")
    own_situation = Situation.__table__.alias("own_situation")
    into_situation = Situation.__table__.alias("into_situation")
    statement = (
        select(
            WatchlistItem.id,
            WatchlistItem.kind,
            WatchlistItem.entity_id,
            WatchlistItem.event_id,
            WatchlistItem.instrument_id,
            WatchlistItem.situation_id,
            WatchlistItem.value,
            WatchlistItem.created_at,
            own_entity.c.name.label("entity_name"),
            own_entity.c.kind.label("entity_kind"),
            into_entity.c.id.label("entity_merged_into_id"),
            into_entity.c.name.label("entity_merged_into_name"),
            own_event.c.title.label("event_title"),
            into_event.c.id.label("event_merged_into_id"),
            into_event.c.title.label("event_merged_into_title"),
            Instrument.name.label("instrument_name"),
            Instrument.symbol.label("instrument_symbol"),
            linked_entity.c.id.label("instrument_entity_id"),
            linked_entity.c.name.label("instrument_entity_name"),
            own_situation.c.name.label("situation_name"),
            into_situation.c.id.label("situation_merged_into_id"),
            into_situation.c.name.label("situation_merged_into_name"),
        )
        .outerjoin(own_entity, own_entity.c.id == WatchlistItem.entity_id)
        .outerjoin(into_entity, into_entity.c.id == own_entity.c.merged_into_id)
        .outerjoin(own_event, own_event.c.id == WatchlistItem.event_id)
        .outerjoin(into_event, into_event.c.id == own_event.c.merged_into_id)
        .outerjoin(Instrument, Instrument.id == WatchlistItem.instrument_id)
        .outerjoin(linked_entity, linked_entity.c.id == Instrument.entity_id)
        .outerjoin(own_situation, own_situation.c.id == WatchlistItem.situation_id)
        .outerjoin(into_situation, into_situation.c.id == own_situation.c.merged_into_id)
        .where(WatchlistItem.watchlist_id == watchlist_id)
        .order_by(WatchlistItem.created_at, WatchlistItem.id)
    )
    return connection.execute(statement).all()


def remove_items(connection: Connection, watchlist_id: int, item_ids: list[int]) -> int:
    """Removes those of the watchlist only; returns how many."""
    statement = delete(WatchlistItem).where(WatchlistItem.watchlist_id == watchlist_id, WatchlistItem.id.in_(item_ids))
    removed = connection.execute(statement).rowcount
    if removed:
        connection.execute(update(Watchlist).where(Watchlist.id == watchlist_id).values(updated_at=func.now()))
    return removed


def lock_items(connection: Connection, watchlist_id: int, item_ids: list[int]) -> list[int]:
    """The ids among these that are items of the watchlist, locked until the end of the transaction."""
    statement = (
        select(WatchlistItem.id)
        .where(WatchlistItem.watchlist_id == watchlist_id, WatchlistItem.id.in_(item_ids))
        .order_by(WatchlistItem.id)
        .with_for_update()
    )
    return list(connection.execute(statement).scalars())


def containing(connection: Connection, kind: str, target: int) -> list[int]:
    """The watchlists that follow this entity, event or Wertpapier."""
    column = {
        WatchItemKind.ENTITY: WatchlistItem.entity_id, WatchItemKind.EVENT: WatchlistItem.event_id,
        WatchItemKind.INSTRUMENT: WatchlistItem.instrument_id, WatchItemKind.SITUATION: WatchlistItem.situation_id,
    }[kind]
    statement = select(WatchlistItem.watchlist_id).where(WatchlistItem.kind == kind, column == literal(target))
    return list(connection.execute(statement).scalars())


@contextmanager
def _name_must_be_free(connection: Connection) -> Iterator[None]:
    # The savepoint keeps the caller's transaction usable after the unique violation.
    try:
        with connection.begin_nested():
            yield
    except IntegrityError as error:
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == UNIQUE_NAME_INDEX:
            raise NameTaken from None
        raise
