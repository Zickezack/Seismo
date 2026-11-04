"""Data access for the Wirkungsannahmen (impact_service): the events due for an assessment, what the LLM is shown of
an event, the assessments, and the hypotheses with their versions - appended, never changed (datamodel.md, rule 3)."""
from collections.abc import Iterable, Sequence
from datetime import timedelta

from sqlalchemy import Connection, Row, String, cast, exists, func, insert, literal, literal_column, or_, select, text
from sqlalchemy.orm import aliased

from app.db.models import (
    Article,
    Entity,
    EntityAlias,
    EntityMention,
    Event,
    EventCandidate,
    Evidence,
    ImpactAssessment,
    ImpactHypothesis,
    ImpactHypothesisVersion,
    Job,
    Source,
    Statement,
)
from app.repositories.source_repository import NAME_COLLATION

IMPACT_LOCK_NAMESPACE = 7308
# An effect on a company, a sector, a commodity or a market a report names (event_service.ECONOMIC_TARGETS), as a
# JSON path - a literal, so it is one expression whatever the call.
_ECONOMIC_IMPACT = literal_column(
    "'$[*] ? (@.target_kind == \"company\" || @.target_kind == \"sector\" || @.target_kind == \"commodity\" "
    "|| @.target_kind == \"market\")'::jsonpath"
)
_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)


def lock(connection: Connection, event_id: int) -> None:
    """Serialises the writes of one event's hypotheses: an assessment's effect and the analyst's reviews."""
    statement = text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), CAST(:key AS integer))")
    connection.execute(statement, {"namespace": IMPACT_LOCK_NAMESPACE, "key": event_id % 2_000_000_000})


def report_count(event_id_column):
    return select(func.count()).where(EventCandidate.event_id == event_id_column).scalar_subquery()


def due(connection: Connection, job_kind: str, target_kinds: Sequence[str], again_after: timedelta, limit: int) -> list[int]:
    """Events not merged whose reports name an economic effect and an entity it may be about, never assessed or with
    a report more than when last assessed - and not asked within `again_after` (a job of `job_kind` queued for it,
    whatever became of it: a run that fails is not asked again at once); the latest reported first."""
    economic = (
        select(EventCandidate.id)
        .where(EventCandidate.event_id == Event.id, EventCandidate.impacts.op("@?")(_ECONOMIC_IMPACT))
        .exists()
    )
    named = (
        select(EntityMention.id)
        .join(EventCandidate, EventCandidate.id == EntityMention.candidate_id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(EventCandidate.event_id == Event.id, Entity.kind.in_(list(target_kinds)))
        .exists()
    )
    latest = (
        select(ImpactAssessment.reports, ImpactAssessment.created_at)
        .where(ImpactAssessment.event_id == Event.id)
        .order_by(ImpactAssessment.created_at.desc(), ImpactAssessment.id.desc())
        .limit(1)
        .lateral()
    )
    asked = (
        select(Job.id)
        .where(Job.dedupe_key == literal(f"{job_kind}:") + cast(Event.id, String), Job.created_at >= func.now() - again_after)
        .exists()
    )
    statement = (
        select(Event.id)
        .outerjoin(latest, literal_column("true"))
        .where(
            Event.merged_into_id.is_(None), economic, named, ~asked,
            or_(latest.c.reports.is_(None), report_count(Event.id) > latest.c.reports),
        )
        .order_by(Event.last_reported_at.desc(), Event.id)
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def event(connection: Connection, event_id: int) -> Row | None:
    statement = select(Event.__table__, report_count(Event.id).label("reports")).where(Event.id == event_id)
    return connection.execute(statement).one_or_none()


def reports(connection: Connection, event_id: int, limit: int) -> Sequence[Row]:
    """The event's latest reports with their impacts and article, the latest first."""
    statement = (
        select(
            EventCandidate.id, EventCandidate.title, EventCandidate.summary, EventCandidate.impacts,
            Article.id.label("article_id"), Article.title.label("article_title"), Article.url.label("article_url"),
            _MOMENT.label("moment"), func.coalesce(Source.name, Article.source_name).label("source_display_name"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.event_id == event_id)
        .order_by(_MOMENT.desc(), EventCandidate.id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def reports_by_id(connection: Connection, candidate_ids: Iterable[int]) -> dict[int, Row]:
    """Reports by id, with their impacts and article: what a cited Beleg points to, wherever its report went since."""
    statement = (
        select(
            EventCandidate.id, EventCandidate.title, EventCandidate.impacts, EventCandidate.event_id,
            Article.id.label("article_id"), Article.url.label("article_url"),
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.id.in_(list(candidate_ids)))
    )
    return {row.id: row for row in connection.execute(statement)}


def statements(connection: Connection, candidate_ids: Iterable[int] | None = None, statement_ids: Iterable[int] | None = None) -> Sequence[Row]:
    """Statements of these reports, or these statements, each with one quote: id, candidate_id, kind, text, quote."""
    quote = select(func.min(Evidence.quote)).where(Evidence.statement_id == Statement.id).scalar_subquery()
    statement = select(Statement.id, Statement.candidate_id, Statement.kind, Statement.text, quote.label("quote"))
    if candidate_ids is not None:
        statement = statement.where(Statement.candidate_id.in_(list(candidate_ids)))
    if statement_ids is not None:
        statement = statement.where(Statement.id.in_(list(statement_ids)))
    return connection.execute(statement.order_by(Statement.candidate_id, Statement.position)).all()


def impact_targets(connection: Connection, candidate_ids: Iterable[int]) -> dict[tuple[int, int], int]:
    """The entity each report's impact is about, by (report, the impact's position): where the recognition linked it."""
    statement = (
        select(EntityMention.candidate_id, EntityMention.position, EntityAlias.entity_id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .where(EntityMention.candidate_id.in_(list(candidate_ids)), EntityMention.source == "impact")
    )
    return {(row.candidate_id, row.position): row.entity_id for row in connection.execute(statement)}


def insert_assessment(connection: Connection, values: dict) -> int:
    return connection.execute(insert(ImpactAssessment).values(values).returning(ImpactAssessment.id)).scalar_one()


def latest_assessment(connection: Connection, event_id: int) -> Row | None:
    statement = (
        select(ImpactAssessment.__table__)
        .where(ImpactAssessment.event_id == event_id)
        .order_by(ImpactAssessment.created_at.desc(), ImpactAssessment.id.desc())
        .limit(1)
    )
    return connection.execute(statement).one_or_none()


def insert_hypothesis(connection: Connection, values: dict) -> int:
    return connection.execute(insert(ImpactHypothesis).values(values).returning(ImpactHypothesis.id)).scalar_one()


def insert_version(connection: Connection, hypothesis_id: int, values: dict) -> int:
    """The next version of the hypothesis: its number one more than the latest."""
    number = (
        select(func.coalesce(func.max(ImpactHypothesisVersion.number), 0) + 1)
        .where(ImpactHypothesisVersion.hypothesis_id == hypothesis_id)
        .scalar_subquery()
    )
    statement = insert(ImpactHypothesisVersion).values(hypothesis_id=hypothesis_id, number=number, **values)
    return connection.execute(statement.returning(ImpactHypothesisVersion.id)).scalar_one()


def _latest_version():
    later = aliased(ImpactHypothesisVersion)
    newer = select(later.id).where(later.hypothesis_id == ImpactHypothesisVersion.hypothesis_id, later.number > ImpactHypothesisVersion.number).exists()
    return ~newer


_TARGET = aliased(Entity)


def _hypotheses():
    target, via = _TARGET, aliased(Entity)
    return (
        select(
            ImpactHypothesis.__table__,
            ImpactHypothesisVersion.id.label("version_id"),
            ImpactHypothesisVersion.number,
            ImpactHypothesisVersion.status,
            ImpactHypothesisVersion.direction,
            ImpactHypothesisVersion.horizon,
            ImpactHypothesisVersion.mechanism,
            ImpactHypothesisVersion.counterarguments,
            ImpactHypothesisVersion.classification,
            ImpactHypothesisVersion.proposed_classification,
            ImpactHypothesisVersion.support,
            ImpactHypothesisVersion.against,
            ImpactHypothesisVersion.origin.label("version_origin"),
            ImpactHypothesisVersion.note,
            ImpactHypothesisVersion.created_at.label("changed_at"),
            target.name.label("target_name"), target.kind.label("target_kind"), target.merged_into_id.label("target_merged_into_id"),
            via.name.label("via_name"),
        )
        .join(ImpactHypothesisVersion, ImpactHypothesisVersion.hypothesis_id == ImpactHypothesis.id)
        .join(target, target.id == ImpactHypothesis.target_entity_id)
        .outerjoin(via, via.id == ImpactHypothesis.via_entity_id)
        .where(_latest_version())
    )


def hypotheses(connection: Connection, event_id: int) -> Sequence[Row]:
    """The event's hypotheses in their latest version, by the target's name."""
    statement = _hypotheses().where(ImpactHypothesis.event_id == event_id).order_by(
        _TARGET.name.collate(NAME_COLLATION), ImpactHypothesis.id
    )
    return connection.execute(statement).all()


def hypotheses_by_id(connection: Connection, hypothesis_ids: Iterable[int]) -> dict[int, Row]:
    statement = _hypotheses().where(ImpactHypothesis.id.in_(list(hypothesis_ids)))
    return {row.id: row for row in connection.execute(statement)}


def of_entity(connection: Connection, entity_id: int, statuses: Iterable[str], limit: int) -> Sequence[Row]:
    """The hypotheses about the entity in one of these states, with their event's title, the latest changed first."""
    statement = (
        _hypotheses()
        .add_columns(Event.title.label("event_title"), Event.last_reported_at)
        .join(Event, Event.id == ImpactHypothesis.event_id)
        .where(ImpactHypothesis.target_entity_id == entity_id, ImpactHypothesisVersion.status.in_(list(statuses)))
        .order_by(ImpactHypothesisVersion.created_at.desc(), ImpactHypothesis.id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def versions(connection: Connection, hypothesis_id: int) -> Sequence[Row]:
    """Every version of the hypothesis with the assessment it came from, the earliest first."""
    statement = (
        select(ImpactHypothesisVersion.__table__, ImpactAssessment.model, ImpactAssessment.prompt_version)
        .outerjoin(ImpactAssessment, ImpactAssessment.id == ImpactHypothesisVersion.assessment_id)
        .where(ImpactHypothesisVersion.hypothesis_id == hypothesis_id)
        .order_by(ImpactHypothesisVersion.number)
    )
    return connection.execute(statement).all()


def has_hypotheses(connection: Connection, event_id: int) -> bool:
    return connection.execute(select(exists().where(ImpactHypothesis.event_id == event_id))).scalar_one()
