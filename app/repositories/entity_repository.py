"""Data access for the entities (Entitäten), their name forms and identifiers (Namensformen), where reports name them
(Nennungen) and the recognition runs (Entitätenerkennung)."""
from collections.abc import Iterable, Sequence

from sqlalchemy import Connection, Row, and_, case, exists, func, insert, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.db.models import (
    AliasScheme,
    Article,
    Entity,
    EntityAlias,
    EntityMention,
    EntityRecognition,
    Event,
    EventCandidate,
    Extraction,
    Job,
    MentionSource,
    Source,
    ValidationResult,
)
from app.repositories.source_repository import NAME_COLLATION

# Of the two-key advisory lock that serialises every write of entities and name forms (7301 LLM slots, 7302 duplicate
# checks, 7303 event assignments).
ENTITY_LOCK_NAMESPACE = 7304

_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)
_LIVE = Entity.merged_into_id.is_(None)
_IS_NAME = EntityAlias.scheme == AliasScheme.NAME


def lock_entities(connection: Connection) -> None:
    """Until the end of the transaction no other one writes entities or name forms: two recognitions never found two
    entities for one name, and a correction never moves a name form a recognition is adding to."""
    statement = text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), 0)")
    connection.execute(statement, {"namespace": ENTITY_LOCK_NAMESPACE})


def due(connection: Connection, job_kind: str, limit: int) -> list[int]:
    """Extractions with reports, without a recognition and never queued, the earliest article first."""
    statement = (
        select(Extraction.id)
        .join(Article, Article.id == Extraction.article_id)
        .where(
            Extraction.validation_result == ValidationResult.VALID,
            exists().where(EventCandidate.extraction_id == Extraction.id),
            ~exists().where(EntityRecognition.extraction_id == Extraction.id),
            # Any job of it, failed ones too: a failed one comes back only when the admin requeues it.
            ~exists().where(Job.dedupe_key == func.concat(literal(f"{job_kind}:"), Extraction.id)),
        )
        .order_by(_MOMENT, Extraction.id)
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def count_unrecognised(connection: Connection) -> int:
    statement = select(func.count()).select_from(Extraction).where(
        Extraction.validation_result == ValidationResult.VALID,
        exists().where(EventCandidate.extraction_id == Extraction.id),
        ~exists().where(EntityRecognition.extraction_id == Extraction.id),
    )
    return connection.execute(statement).scalar_one()


def recognised(connection: Connection, extraction_id: int) -> bool:
    return connection.execute(select(exists().where(EntityRecognition.extraction_id == extraction_id))).scalar_one()


def insert_recognition(connection: Connection, values: dict) -> bool:
    """The run; False when the extraction has one already."""
    statement = pg_insert(EntityRecognition).values(values).on_conflict_do_nothing().returning(EntityRecognition.extraction_id)
    return connection.execute(statement).first() is not None


def similar(connection: Connection, key: str, limit: int) -> list[tuple[int, float]]:
    """The entities with a name form similar to the key (pg_trgm, from its similarity threshold 0.3), the most similar
    first, with that similarity."""
    score = func.max(func.similarity(EntityAlias.key, key)).label("score")
    statement = (
        select(EntityAlias.entity_id, score)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(_IS_NAME, EntityAlias.key.op("%")(key), _LIVE)
        .group_by(EntityAlias.entity_id)
        .order_by(score.desc(), EntityAlias.entity_id)
        .limit(limit)
    )
    return [(row.entity_id, row.score) for row in connection.execute(statement)]


def similar_to_entity(connection: Connection, entity_id: int, kinds: Iterable[str], limit: int) -> list[tuple[int, float]]:
    """The other entities of these kinds by the similarity of their name forms to the entity's, the most similar
    first: an entity scores its name form most similar to any of the entity's."""
    own = aliased(EntityAlias)
    score = func.max(func.similarity(EntityAlias.key, own.key)).label("score")
    statement = (
        select(EntityAlias.entity_id, score)
        .join(own, and_(own.entity_id == entity_id, own.scheme == AliasScheme.NAME, EntityAlias.key.op("%")(own.key)))
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(_IS_NAME, EntityAlias.entity_id != entity_id, _LIVE, Entity.kind.in_(list(kinds)))
        .group_by(EntityAlias.entity_id)
        .order_by(score.desc(), EntityAlias.entity_id)
        .limit(limit)
    )
    return [(row.entity_id, row.score) for row in connection.execute(statement)]


def best_similarity(connection: Connection, entity_id: int, key: str) -> float | None:
    """The key's similarity to the entity's most similar name form."""
    statement = select(func.max(func.similarity(EntityAlias.key, key))).where(EntityAlias.entity_id == entity_id, _IS_NAME)
    return connection.execute(statement).scalar_one()


def get(connection: Connection, entity_id: int) -> Row | None:
    return connection.execute(select(Entity.__table__).where(Entity.id == entity_id)).one_or_none()


def entities(connection: Connection, entity_ids: Iterable[int]) -> dict[int, Row]:
    statement = select(Entity.__table__).where(Entity.id.in_(list(entity_ids)))
    return {row.id: row for row in connection.execute(statement)}


def name_forms(connection: Connection, entity_ids: Iterable[int]) -> dict[int, list[str]]:
    """The name forms of the entities, the earliest first."""
    statement = (
        select(EntityAlias.entity_id, EntityAlias.value)
        .where(EntityAlias.entity_id.in_(list(entity_ids)), _IS_NAME)
        .order_by(EntityAlias.id)
    )
    forms: dict[int, list[str]] = {}
    for row in connection.execute(statement):
        forms.setdefault(row.entity_id, []).append(row.value)
    return forms


def find_identifier(connection: Connection, scheme: str, key: str) -> Row | None:
    """The alias with this identifier, with its entity."""
    statement = (
        select(EntityAlias.id, EntityAlias.entity_id, EntityAlias.uncertain)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(EntityAlias.scheme == scheme, EntityAlias.key == key, _LIVE)
    )
    return connection.execute(statement).one_or_none()


def find_names(connection: Connection, keys: Iterable[str], kinds: Iterable[str]) -> Sequence[Row]:
    """The name forms with these keys of entities of these kinds, the certain ones first, then the earliest."""
    statement = (
        select(EntityAlias.id, EntityAlias.entity_id, EntityAlias.key, EntityAlias.uncertain)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(_IS_NAME, EntityAlias.key.in_(list(keys)), Entity.kind.in_(list(kinds)), _LIVE)
        .order_by(EntityAlias.uncertain, EntityAlias.id)
    )
    return connection.execute(statement).all()


def alias_keys(connection: Connection, entity_id: int) -> dict[tuple[str, str], Row]:
    """The entity's aliases by scheme and key."""
    statement = select(EntityAlias.id, EntityAlias.scheme, EntityAlias.key, EntityAlias.uncertain).where(EntityAlias.entity_id == entity_id)
    return {(row.scheme, row.key): row for row in connection.execute(statement)}


def identifier_taken(connection: Connection, scheme: str, key: str) -> bool:
    return connection.execute(select(exists().where(EntityAlias.scheme == scheme, EntityAlias.key == key))).scalar_one()


def create_entity(connection: Connection, values: dict) -> int:
    return connection.execute(insert(Entity).values(values).returning(Entity.id)).scalar_one()


def insert_alias(connection: Connection, values: dict) -> int:
    return connection.execute(insert(EntityAlias).values(values).returning(EntityAlias.id)).scalar_one()


def set_parent(connection: Connection, entity_id: int, parent_id: int) -> None:
    """The entity's parent, unless it has one: a recognition never replaces it."""
    connection.execute(update(Entity).where(Entity.id == entity_id, Entity.parent_id.is_(None)).values(parent_id=parent_id))


def insert_mentions(connection: Connection, rows: list[dict]) -> None:
    if rows:
        connection.execute(insert(EntityMention).values(rows))


def catalogue(connection: Connection) -> dict[str, int]:
    """The entities of the starter list by their item's key."""
    statement = (
        select(EntityAlias.key, EntityAlias.entity_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(EntityAlias.scheme == AliasScheme.CATALOGUE, _LIVE)
    )
    return {row.key: row.entity_id for row in connection.execute(statement)}


def _aliases():
    """Per entity: its aliases, and the uncertain ones."""
    return (
        select(
            EntityAlias.entity_id,
            func.count().label("alias_count"),
            func.count().filter(EntityAlias.uncertain).label("uncertain_count"),
        )
        .group_by(EntityAlias.entity_id)
        .subquery()
    )


def _named():
    """Per entity: the reports that name it, and their events."""
    return (
        select(
            EntityAlias.entity_id,
            func.count(func.distinct(EntityMention.candidate_id)).label("report_count"),
            func.count(func.distinct(EventCandidate.event_id)).label("event_count"),
        )
        .join(EntityMention, EntityMention.alias_id == EntityAlias.id)
        .join(EventCandidate, EventCandidate.id == EntityMention.candidate_id)
        .group_by(EntityAlias.entity_id)
        .subquery()
    )


def _listing(kind: str | None, uncertain_only: bool, search: str | None):
    aliases, named = _aliases(), _named()
    statement = (
        select(
            Entity.id,
            Entity.kind,
            Entity.name,
            func.coalesce(aliases.c.alias_count, 0).label("alias_count"),
            func.coalesce(aliases.c.uncertain_count, 0).label("uncertain_count"),
            func.coalesce(named.c.report_count, 0).label("report_count"),
            func.coalesce(named.c.event_count, 0).label("event_count"),
        )
        .outerjoin(aliases, aliases.c.entity_id == Entity.id)
        .outerjoin(named, named.c.entity_id == Entity.id)
        .where(_LIVE)
    )
    if kind:
        statement = statement.where(Entity.kind == kind)
    if uncertain_only:
        statement = statement.where(aliases.c.uncertain_count > 0)
    if search:
        found = exists().where(EntityAlias.entity_id == Entity.id, func.strpos(func.lower(EntityAlias.value), search.lower()) > 0)
        statement = statement.where(found)
    return statement


def listing(connection: Connection, kind: str | None, uncertain_only: bool, search: str | None, limit: int) -> Sequence[Row]:
    """The entities, not merged ones, by name: of one kind, with uncertain name forms only, or with a name form that
    contains the search, regardless of case."""
    statement = _listing(kind, uncertain_only, search).order_by(Entity.name.collate(NAME_COLLATION), Entity.id).limit(limit)
    return connection.execute(statement).all()


def count_listing(connection: Connection, kind: str | None, uncertain_only: bool, search: str | None) -> int:
    statement = select(func.count()).select_from(_listing(kind, uncertain_only, search).subquery())
    return connection.execute(statement).scalar_one()


def listed(connection: Connection, entity_ids: Iterable[int]) -> Sequence[Row]:
    """These entities as the list shows them; a merged one is left out."""
    return connection.execute(_listing(None, False, None).where(Entity.id.in_(list(entity_ids)))).all()


def aliases(connection: Connection, entity_id: int) -> Sequence[Row]:
    """The entity's name forms and identifiers, names first, each with the reports that name it by it."""
    mentions = select(func.count()).where(EntityMention.alias_id == EntityAlias.id).scalar_subquery()
    statement = (
        select(EntityAlias.__table__, mentions.label("mention_count"))
        .where(EntityAlias.entity_id == entity_id)
        .order_by(case((_IS_NAME, 0), else_=1), EntityAlias.id)
    )
    return connection.execute(statement).all()


def children(connection: Connection, entity_id: int) -> Sequence[Row]:
    """Its subsidiaries and products, by name."""
    statement = (
        select(Entity.id, Entity.kind, Entity.name)
        .where(Entity.parent_id == entity_id, _LIVE)
        .order_by(Entity.name.collate(NAME_COLLATION), Entity.id)
    )
    return connection.execute(statement).all()


def mentions(connection: Connection, entity_id: int, limit: int) -> Sequence[Row]:
    """The reports that name the entity, the latest first: report, event, article and source, the name form, and what
    the report says of it - the actor or impact as extracted."""
    detail = case((EntityMention.source == MentionSource.ACTOR, EventCandidate.actors), else_=EventCandidate.impacts)
    statement = (
        select(
            EntityMention.id,
            EntityMention.source,
            detail.op("->", return_type=JSONB)(EntityMention.position).label("detail"),
            EntityAlias.value.label("alias_value"),
            EntityAlias.uncertain,
            EventCandidate.id.label("candidate_id"),
            EventCandidate.title.label("report_title"),
            EventCandidate.event_id,
            Event.title.label("event_title"),
            Article.id.label("article_id"),
            _MOMENT.label("moment"),
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
        )
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(EventCandidate, EventCandidate.id == EntityMention.candidate_id)
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(Event, Event.id == EventCandidate.event_id)
        .where(EntityAlias.entity_id == entity_id)
        .order_by(_MOMENT.desc(), EntityMention.id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def count_mentions(connection: Connection, entity_id: int) -> int:
    statement = (
        select(func.count())
        .select_from(EntityMention)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .where(EntityAlias.entity_id == entity_id)
    )
    return connection.execute(statement).scalar_one()


def of_event(connection: Connection, event_id: int) -> Sequence[Row]:
    """The entities the event's reports name, the most often named first: how many reports name each, by which name
    forms, and whether by an uncertain one."""
    report_count = func.count(func.distinct(EntityMention.candidate_id))
    statement = (
        select(
            Entity.id,
            Entity.kind,
            Entity.name,
            report_count.label("report_count"),
            func.bool_or(EntityAlias.uncertain).label("uncertain"),
            func.array_agg(func.distinct(EntityAlias.value)).label("forms"),
        )
        .select_from(EntityMention)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .join(EventCandidate, EventCandidate.id == EntityMention.candidate_id)
        .where(EventCandidate.event_id == event_id)
        .group_by(Entity.id)
        .order_by(report_count.desc(), Entity.name.collate(NAME_COLLATION), Entity.id)
    )
    return connection.execute(statement).all()


def aliases_by_id(connection: Connection, alias_ids: Iterable[int]) -> dict[int, Row]:
    statement = select(EntityAlias.__table__).where(EntityAlias.id.in_(list(alias_ids)))
    return {row.id: row for row in connection.execute(statement)}


def confirm(connection: Connection, alias_ids: Iterable[int]) -> None:
    statement = update(EntityAlias).where(EntityAlias.id.in_(list(alias_ids)), EntityAlias.uncertain)
    connection.execute(statement.values(uncertain=False, confirmed_at=func.now()))


def move_aliases(connection: Connection, alias_ids: Iterable[int], entity_id: int, values: dict | None = None) -> None:
    statement = update(EntityAlias).where(EntityAlias.id.in_(list(alias_ids)))
    connection.execute(statement.values({"entity_id": entity_id} | (values or {})))


def merge_into(connection: Connection, entity_ids: list[int], into_id: int) -> int:
    """Their name forms, identifiers, subsidiaries and products go to the other entity, and they are marked merged into
    it; the number of name forms moved."""
    moved = connection.execute(
        update(EntityAlias).where(EntityAlias.entity_id.in_(entity_ids)).values(entity_id=into_id).returning(EntityAlias.id)
    ).all()
    connection.execute(update(Entity).where(Entity.parent_id.in_(entity_ids)).values(parent_id=into_id))
    # Itself a subsidiary of one of them: no parent of its own.
    connection.execute(update(Entity).where(Entity.id == into_id, Entity.parent_id == into_id).values(parent_id=None))
    connection.execute(update(Entity).where(Entity.id.in_(entity_ids)).values(merged_into_id=into_id, parent_id=None))
    return len(moved)


def take_parent(connection: Connection, entity_id: int, entity_ids: list[int]) -> None:
    """The parent of the first of these entities that has one, unless the entity has one."""
    parent = (
        select(Entity.parent_id)
        .where(Entity.id.in_(entity_ids), Entity.parent_id.is_not(None), Entity.parent_id != entity_id)
        .order_by(Entity.id)
        .limit(1)
        .scalar_subquery()
    )
    connection.execute(update(Entity).where(Entity.id == entity_id, Entity.parent_id.is_(None)).values(parent_id=parent))



def live_by_name(connection: Connection, name: str) -> list[int]:
    """The entities, not merged ones, whose name or a name form is this one, regardless of case."""
    lowered = name.lower()
    statement = (
        select(Entity.id)
        .where(_LIVE, (func.lower(Entity.name) == lowered) | exists().where(
            EntityAlias.entity_id == Entity.id, _IS_NAME, func.lower(EntityAlias.value) == lowered
        ))
        .order_by(Entity.id)
    )
    return list(connection.execute(statement).scalars())


def choices(connection: Connection, limit: int) -> Sequence[Row]:
    """Names and numbers of the entities, not merged ones, by name: what a form offers to choose from."""
    statement = select(Entity.id, Entity.name).where(_LIVE).order_by(Entity.name.collate(NAME_COLLATION), Entity.id).limit(limit)
    return connection.execute(statement).all()
