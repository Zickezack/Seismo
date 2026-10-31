"""Data access for the relationships between entities (Beziehungen) and their Angaben."""
from collections.abc import Iterable, Sequence

from sqlalchemy import Connection, Row, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.db.models import (
    Article,
    Entity,
    EntityAlias,
    EntityRelationship,
    Evidence,
    EventCandidate,
    RecordOrigin,
    RelationshipRecord,
    Source,
    Statement,
)
from app.repositories.source_repository import NAME_COLLATION

_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)


def get_or_create(connection: Connection, kind: str, subject_id: int, object_id: int) -> int:
    """The relationship's id, made when there is none yet."""
    statement = (
        pg_insert(EntityRelationship)
        .values(kind=kind, subject_id=subject_id, object_id=object_id)
        .on_conflict_do_nothing()
        .returning(EntityRelationship.id)
    )
    if (made := connection.execute(statement).scalar_one_or_none()) is not None:
        return made
    existing = select(EntityRelationship.id).where(
        EntityRelationship.kind == kind, EntityRelationship.subject_id == subject_id, EntityRelationship.object_id == object_id
    )
    return connection.execute(existing).scalar_one()


def insert_record(connection: Connection, values: dict) -> int:
    return connection.execute(insert(RelationshipRecord).values(values).returning(RelationshipRecord.id)).scalar_one()


def statements_of(connection: Connection, extraction_id: int) -> Sequence[Row]:
    """The statements of an extraction's reports with their quotes and their report's time, in order: what the
    recognition may cite as a relationship's evidence."""
    statement = (
        select(Statement.id, Statement.candidate_id, Statement.text, Evidence.quote, _MOMENT.label("moment"))
        .join(EventCandidate, EventCandidate.id == Statement.candidate_id)
        .join(Evidence, Evidence.statement_id == Statement.id)
        .join(Article, Article.id == EventCandidate.article_id)
        .where(EventCandidate.extraction_id == extraction_id)
        .order_by(EventCandidate.position, Statement.position)
    )
    return connection.execute(statement).all()


def get(connection: Connection, relationship_id: int) -> Row | None:
    subject, other = aliased(Entity), aliased(Entity)
    statement = (
        select(
            EntityRelationship.__table__,
            subject.name.label("subject_name"), subject.kind.label("subject_kind"),
            other.name.label("object_name"), other.kind.label("object_kind"),
        )
        .join(subject, subject.id == EntityRelationship.subject_id)
        .join(other, other.id == EntityRelationship.object_id)
        .where(EntityRelationship.id == relationship_id)
    )
    return connection.execute(statement).one_or_none()


def of_entity(connection: Connection, entity_id: int) -> Sequence[Row]:
    """The entity's relationships that have Angaben not replaced, with the other entity, by its name."""
    other = aliased(Entity)
    other_id = func.coalesce(
        func.nullif(EntityRelationship.object_id, entity_id), EntityRelationship.subject_id
    )
    has_records = (
        select(RelationshipRecord.id)
        .where(RelationshipRecord.relationship_id == EntityRelationship.id, RelationshipRecord.replaced_at.is_(None))
        .exists()
    )
    statement = (
        select(EntityRelationship.__table__, other.id.label("other_id"), other.name.label("other_name"), other.kind.label("other_kind"))
        .join(other, other.id == other_id)
        .where(or_(EntityRelationship.subject_id == entity_id, EntityRelationship.object_id == entity_id), has_records)
        .order_by(EntityRelationship.kind, other.name.collate(NAME_COLLATION), EntityRelationship.id)
    )
    return connection.execute(statement).all()


def records(connection: Connection, relationship_ids: Iterable[int]) -> Sequence[Row]:
    """The Angaben of the relationships in the order Seismo learned them, each report's with its statement, quote,
    source quality, article and the name forms it gave."""
    subject_alias, object_alias = aliased(EntityAlias), aliased(EntityAlias)
    statement = (
        select(
            RelationshipRecord.__table__,
            Statement.text.label("statement_text"),
            Evidence.quote,
            Evidence.source_quality,
            EventCandidate.title.label("report_title"),
            EventCandidate.event_id,
            Article.id.label("article_id"),
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
            subject_alias.value.label("subject_form"),
            subject_alias.uncertain.label("subject_uncertain"),
            object_alias.value.label("object_form"),
            object_alias.uncertain.label("object_uncertain"),
        )
        .outerjoin(Statement, Statement.id == RelationshipRecord.statement_id)
        .outerjoin(Evidence, Evidence.statement_id == Statement.id)
        .outerjoin(EventCandidate, EventCandidate.id == RelationshipRecord.candidate_id)
        .outerjoin(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(subject_alias, subject_alias.id == RelationshipRecord.subject_alias_id)
        .outerjoin(object_alias, object_alias.id == RelationshipRecord.object_alias_id)
        .where(RelationshipRecord.relationship_id.in_(list(relationship_ids)))
        .order_by(RelationshipRecord.recorded_at, RelationshipRecord.id)
    )
    return connection.execute(statement).all()


def involving(connection: Connection, entity_ids: Iterable[int]) -> Sequence[Row]:
    """The relationships with one of these entities on either side."""
    entity_ids = list(entity_ids)
    statement = select(EntityRelationship.__table__).where(
        or_(EntityRelationship.subject_id.in_(entity_ids), EntityRelationship.object_id.in_(entity_ids))
    )
    return connection.execute(statement).all()


def reports_naming(connection: Connection, alias_ids: Iterable[int]) -> Sequence[Row]:
    """The reports' Angaben that name one side by one of these name forms, with their relationship."""
    alias_ids = list(alias_ids)
    statement = (
        select(
            RelationshipRecord.id, RelationshipRecord.subject_alias_id, RelationshipRecord.object_alias_id,
            EntityRelationship.kind, EntityRelationship.subject_id, EntityRelationship.object_id,
        )
        .join(EntityRelationship, EntityRelationship.id == RelationshipRecord.relationship_id)
        .where(
            RelationshipRecord.origin == RecordOrigin.REPORT,
            or_(RelationshipRecord.subject_alias_id.in_(alias_ids), RelationshipRecord.object_alias_id.in_(alias_ids)),
        )
    )
    return connection.execute(statement).all()


def move_records(connection: Connection, record_ids: Iterable[int], relationship_id: int, swap_sides: bool = False) -> None:
    """The Angaben go to the other relationship; `swap_sides`: its subject is their object - a competition, whose
    subject is the lower id."""
    values = {"relationship_id": relationship_id}
    if swap_sides:
        values |= {"subject_alias_id": RelationshipRecord.object_alias_id, "object_alias_id": RelationshipRecord.subject_alias_id}
    connection.execute(update(RelationshipRecord).where(RelationshipRecord.id.in_(list(record_ids))).values(values))


def mark_replaced(connection: Connection, extraction_id: int) -> None:
    """The reports' Angaben of the extraction no longer count: their reading was replaced - the article extracted
    again, or the extraction's entities recognised again. They stay, marked with the time."""
    candidate_ids = select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id)
    connection.execute(
        update(RelationshipRecord)
        .where(
            RelationshipRecord.origin == RecordOrigin.REPORT,
            RelationshipRecord.candidate_id.in_(candidate_ids),
            RelationshipRecord.replaced_at.is_(None),
        )
        .values(replaced_at=func.now())
    )


def record_ids(connection: Connection, relationship_id: int) -> list[int]:
    statement = select(RelationshipRecord.id).where(RelationshipRecord.relationship_id == relationship_id)
    return list(connection.execute(statement).scalars())
