"""Data access for the event study: which Wertpapiere with daily prices the events' reports name, and which sectors."""
from collections.abc import Iterable, Sequence

from sqlalchemy import Connection, Row, and_, func, select
from sqlalchemy.orm import aliased

from app.db.models import (
    AliasScheme,
    Entity,
    EntityAlias,
    EntityKind,
    EntityMention,
    Event,
    EventCandidate,
    Instrument,
    PriceSource,
)


def observations(connection: Connection, event_ids: Iterable[int] | None = None) -> Sequence[Row]:
    """Each event - of these, else every one not merged - with each Wertpapier with daily prices (Alpaca's) of an
    entity its reports name, traded or no longer: event_id, event_type, title, first_reported_at, and the Wertpapier's
    id, symbol, name, history_through and delisted_on. By event, then the Wertpapier's symbol."""
    statement = (
        select(
            Event.id.label("event_id"),
            Event.event_type,
            Event.title,
            Event.first_reported_at,
            Instrument.id.label("instrument_id"),
            Instrument.symbol,
            Instrument.name,
            Instrument.history_through,
            Instrument.delisted_on,
        )
        .distinct()
        .select_from(Event)
        .join(EventCandidate, EventCandidate.event_id == Event.id)
        .join(EntityMention, EntityMention.candidate_id == EventCandidate.id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Instrument, Instrument.entity_id == EntityAlias.entity_id)
        .where(Event.merged_into_id.is_(None), Instrument.source == PriceSource.ALPACA, Instrument.history)
        .order_by(Event.id, Instrument.symbol, Instrument.id)
    )
    if event_ids is not None:
        statement = statement.where(Event.id.in_(list(event_ids)))
    return connection.execute(statement).all()


def sectors(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The sectors of the starter list the events' reports name: event_id, the sector's key and name, and how many of
    the event's reports name it."""
    catalogue = aliased(EntityAlias)
    reports = func.count(func.distinct(EntityMention.candidate_id))
    statement = (
        select(EventCandidate.event_id, catalogue.value.label("key"), Entity.name, reports.label("reports"))
        .select_from(EntityMention)
        .join(EventCandidate, EventCandidate.id == EntityMention.candidate_id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .join(catalogue, and_(catalogue.entity_id == Entity.id, catalogue.scheme == AliasScheme.CATALOGUE))
        .where(EventCandidate.event_id.in_(list(event_ids)), Entity.kind == EntityKind.SECTOR)
        .group_by(EventCandidate.event_id, catalogue.value, Entity.id)
    )
    return connection.execute(statement).all()


def first_reported_at(connection: Connection, event_id: int):
    """When the event's first report came out; None for no such event."""
    return connection.execute(select(Event.first_reported_at).where(Event.id == event_id)).scalar_one_or_none()
