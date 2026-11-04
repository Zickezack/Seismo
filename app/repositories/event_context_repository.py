"""Data access for putting events into context (userstories.md, Dashboard, Recherche und Alerts, story 4): what their
reports leave uncertain or contradict, and the earlier events comparable with one."""
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, func, select

from app.db.models import (
    Article,
    Entity,
    EntityAlias,
    EntityMention,
    Event,
    EventCandidate,
    Evidence,
    MentionSource,
    Statement,
)

_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)


def statement_levels(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The statements of the events' reports counted by event, kind and their two levels (datamodel.md, rule 7)."""
    statement = (
        select(
            EventCandidate.event_id, Statement.kind, Statement.extraction_confidence, Evidence.source_quality,
            func.count().label("count"),
        )
        .join(EventCandidate, EventCandidate.id == Statement.candidate_id)
        .join(Evidence, Evidence.statement_id == Statement.id)
        .where(EventCandidate.event_id.in_(list(event_ids)))
        .group_by(EventCandidate.event_id, Statement.kind, Statement.extraction_confidence, Evidence.source_quality)
    )
    return connection.execute(statement).all()


def reports_of(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The events' reports, each event's the earliest first: what they state, how sure the inference is, how they came
    to the event and the impacts they name."""
    statement = (
        select(
            EventCandidate.event_id, EventCandidate.id, EventCandidate.title, EventCandidate.status, _MOMENT.label("moment"),
            EventCandidate.inference_confidence, EventCandidate.assignment_method, EventCandidate.impacts,
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .where(EventCandidate.event_id.in_(list(event_ids)))
        .order_by(EventCandidate.event_id, _MOMENT, EventCandidate.id)
    )
    return connection.execute(statement).all()


def impact_entities(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The entities the events' reports name as an impact's target: by report and the impact's place in it."""
    statement = (
        select(EntityMention.candidate_id, EntityMention.position, Entity.id.label("entity_id"), Entity.name)
        .join(EventCandidate, EventCandidate.id == EntityMention.candidate_id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(EventCandidate.event_id.in_(list(event_ids)), EntityMention.source == MentionSource.IMPACT)
    )
    return connection.execute(statement).all()


def uncertain_forms(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The uncertain name forms the events' reports name entities by (datamodel.md, rule 9), each once per event."""
    statement = (
        select(EventCandidate.event_id, EntityAlias.value, Entity.id.label("entity_id"), Entity.name.label("entity_name"))
        .distinct()
        .join(EntityMention, EntityMention.candidate_id == EventCandidate.id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(EventCandidate.event_id.in_(list(event_ids)), EntityAlias.uncertain)
        .order_by(EventCandidate.event_id, Entity.name, EntityAlias.value)
    )
    return connection.execute(statement).all()


def _earlier(before: datetime, event_id: int):
    """The events, not merged ones, whose last report came before `before`."""
    return (Event.merged_into_id.is_(None), Event.id != event_id, Event.last_reported_at < before)


def earlier_of_type(connection: Connection, event_id: int, before: datetime, event_types: Iterable[str], limit: int) -> list[int]:
    """The latest earlier events of one of the types, by their own (their founding report's)."""
    statement = (
        select(Event.id)
        .where(*_earlier(before, event_id), Event.event_type.in_(list(event_types)))
        .order_by(Event.last_reported_at.desc(), Event.id.desc())
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def earlier_sharing(connection: Connection, event_id: int, before: datetime, entity_ids: Iterable[int], limit: int) -> list[int]:
    """The earlier events whose reports name the most of the entities, the latest first among equals."""
    entity_ids = list(entity_ids)
    if not entity_ids:
        return []
    shared = func.count(func.distinct(EntityAlias.entity_id))
    statement = (
        select(Event.id)
        .join(EventCandidate, EventCandidate.event_id == Event.id)
        .join(EntityMention, EntityMention.candidate_id == EventCandidate.id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .where(*_earlier(before, event_id), EntityAlias.entity_id.in_(entity_ids))
        .group_by(Event.id)
        .order_by(shared.desc(), Event.last_reported_at.desc(), Event.id.desc())
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def facts_of(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The types and countries the events and their reports state, once each."""
    event_ids = list(event_ids)
    own = select(Event.id.label("event_id"), Event.event_type, Event.country).where(Event.id.in_(event_ids))
    reported = select(EventCandidate.event_id, EventCandidate.event_type, EventCandidate.country).where(
        EventCandidate.event_id.in_(event_ids)
    )
    return connection.execute(own.union(reported)).all()


def entity_names(connection: Connection, entity_ids: Iterable[int]) -> dict[int, str]:
    statement = select(Entity.id, Entity.name).where(Entity.id.in_(list(entity_ids)))
    return {row.id: row.name for row in connection.execute(statement)}

