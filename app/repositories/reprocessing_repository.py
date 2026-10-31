"""Data access for reprocessing (Neuverarbeitung): the current runs of a step by schema version and model, which of
them a reprocessing selects and queues, the reprocessings themselves, and the reports of replaced readings still in
their events."""
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Connection, Row, and_, exists, func, insert, literal, or_, select, update

from app.db.models import (
    ACTIVE_JOB_STATUSES,
    Article,
    EntityRecognition,
    EventCandidate,
    Extraction,
    Job,
    JobStatus,
    RawText,
    Reprocessing,
    ReprocessingStatus,
    ReprocessingStep,
    ValidationResult,
)
from app.repositories.extraction_repository import CURRENT

_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)
# An article's day: of its publication, else its fetch, in UTC.
_DAY = func.date(func.timezone("UTC", _MOMENT))


@dataclass(frozen=True)
class Selection:
    """The current runs of a step made with this schema version and model, of the articles of a Quelle (when
    `by_source`; an id gone matches none) and of a period, either end open."""

    step: str
    schema_version: str
    model: str
    by_source: bool = False
    source_id: int | None = None
    period_start: date | None = None
    period_end: date | None = None


def groups(connection: Connection, step: str) -> Sequence[Row]:
    """The step's current runs by schema version and model, with their number: of the extraction the articles'
    current readings, of the recognition those that asked the model (a report naming nobody needs none)."""
    if step == ReprocessingStep.EXTRACTION:
        version, model = Extraction.schema_version, Extraction.model
        statement = select(version, model, func.count().label("count")).where(CURRENT)
    else:
        version, model = EntityRecognition.schema_version, EntityRecognition.model
        statement = (
            select(version, model, func.count().label("count"))
            .join(Extraction, Extraction.id == EntityRecognition.extraction_id)
            .where(CURRENT, EntityRecognition.names > 0)
        )
    statement = statement.group_by(version, model).order_by(version.desc(), model)
    return connection.execute(statement).all()


def _selected(selection: Selection, with_text: bool = True):
    """The runs selected: (extraction id, article id, the article's time). An extraction needs the article's text,
    an original's - its raw text may be gone (`with_text` False: those without it)."""
    if selection.step == ReprocessingStep.EXTRACTION:
        statement = (
            select(Extraction.id.label("extraction_id"), Article.id.label("article_id"), _MOMENT.label("moment"))
            .join(Article, Article.id == Extraction.article_id)
            .outerjoin(RawText, RawText.article_id == Article.id)
            .where(
                CURRENT,
                Extraction.schema_version == selection.schema_version,
                Extraction.model == selection.model,
                Article.duplicate_of_id.is_(None),
            )
        )
        has_text = func.coalesce(func.length(RawText.plain_text), 0) > 0
        statement = statement.where(has_text if with_text else ~has_text)
    else:
        statement = (
            select(Extraction.id.label("extraction_id"), Article.id.label("article_id"), _MOMENT.label("moment"))
            .join(EntityRecognition, EntityRecognition.extraction_id == Extraction.id)
            .join(Article, Article.id == Extraction.article_id)
            .where(
                CURRENT,
                EntityRecognition.names > 0,
                EntityRecognition.schema_version == selection.schema_version,
                EntityRecognition.model == selection.model,
            )
        )
    if selection.by_source:
        # A Quelle deleted since: its id is gone, and so are its articles' - none matches.
        statement = statement.where(Article.source_id == selection.source_id)
    if selection.period_start:
        statement = statement.where(_DAY >= selection.period_start)
    if selection.period_end:
        statement = statement.where(_DAY <= selection.period_end)
    return statement


def count(connection: Connection, selection: Selection) -> tuple[int, int]:
    """How many runs the selection takes, and how many extractions it cannot, their articles' texts being gone."""
    taken = select(func.count()).select_from(_selected(selection).subquery())
    without_text = 0
    if selection.step == ReprocessingStep.EXTRACTION:
        without_text = connection.execute(select(func.count()).select_from(_selected(selection, with_text=False).subquery())).scalar_one()
    return connection.execute(taken).scalar_one(), without_text


def _key_prefix(kind: str, reprocessing_id: int) -> str:
    return f"{kind}:{reprocessing_id}:"


def dedupe_key(kind: str, reprocessing_id: int | None, extraction_id: int) -> str:
    """One job per run and reprocessing; 0 for an article's own."""
    return f"{_key_prefix(kind, reprocessing_id or 0)}{extraction_id}"


def due(connection: Connection, run: Row, kind: str, limit: int) -> Sequence[Row]:
    """The runs the reprocessing selects and never queued, the newest article first."""
    selection = Selection(
        run.step, run.schema_version, run.model, run.source_name is not None, run.source_id, run.period_start, run.period_end
    )
    selected = _selected(selection).subquery()
    queued = exists().where(Job.dedupe_key == func.concat(literal(_key_prefix(kind, run.id)), selected.c.extraction_id))
    statement = (
        select(selected.c.extraction_id, selected.c.article_id)
        .where(~queued)
        .order_by(selected.c.moment.desc(), selected.c.extraction_id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def _jobs_of(kind: str, reprocessing_id, statuses: Iterable[str]):
    """Its jobs of the kind in these statuses; `reprocessing_id` a number or a column."""
    return and_(Job.kind == kind, Job.payload["reprocessing_id"].as_integer() == reprocessing_id, Job.status.in_(list(statuses)))


def has_open_jobs(connection: Connection, kind: str, reprocessing_id: int) -> bool:
    return connection.execute(select(exists().where(_jobs_of(kind, reprocessing_id, ACTIVE_JOB_STATUSES)))).scalar_one()


def jobs_since(connection: Connection, kinds: Iterable[str], period: timedelta) -> int:
    """Jobs of the kinds queued within the period, whatever became of them: the budget's count."""
    statement = select(func.count()).select_from(Job).where(Job.kind.in_(list(kinds)), Job.created_at >= func.now() - period)
    return connection.execute(statement).scalar_one()


def insert_reprocessing(connection: Connection, values: dict) -> int:
    return connection.execute(insert(Reprocessing).values(values).returning(Reprocessing.id)).scalar_one()


def running(connection: Connection) -> Row | None:
    """The reprocessing that runs, the earliest should there be more."""
    statement = (
        select(Reprocessing.__table__)
        .where(Reprocessing.status == ReprocessingStatus.RUNNING)
        .order_by(Reprocessing.created_at, Reprocessing.id)
        .limit(1)
    )
    return connection.execute(statement).one_or_none()


def lock_reprocessings(connection: Connection, reprocessing_ids: list[int]) -> Sequence[Row]:
    """Id and status of the existing ones, locked until the end of the transaction."""
    statement = (
        select(Reprocessing.id, Reprocessing.status)
        .where(Reprocessing.id.in_(reprocessing_ids))
        .order_by(Reprocessing.id)
        .with_for_update()
    )
    return connection.execute(statement).all()


def finish(connection: Connection, reprocessing_ids: list[int], status: str) -> int:
    """End running reprocessings; one that ended before stays as it is."""
    statement = (
        update(Reprocessing)
        .where(Reprocessing.id.in_(reprocessing_ids), Reprocessing.status == ReprocessingStatus.RUNNING)
        .values(status=status, finished_at=func.now())
    )
    return connection.execute(statement).rowcount


def latest(connection: Connection, limit: int, kinds: dict[str, str]) -> Sequence[Row]:
    """The latest reprocessings, newest first, each with the runs it made, and its jobs waiting or running and failed.
    `kinds`: the job kind of each step."""

    def jobs(statuses: Iterable[str]):
        conditions = [
            and_(Reprocessing.step == step, _jobs_of(kind, Reprocessing.id, statuses))
            for step, kind in kinds.items()
        ]
        return select(func.count()).select_from(Job).where(or_(*conditions)).scalar_subquery()

    # A run with an unusable answer is stored with it too: it made none.
    made_extractions = (
        select(func.count())
        .where(Extraction.reprocessing_id == Reprocessing.id, Extraction.validation_result == ValidationResult.VALID)
        .scalar_subquery()
    )
    made_recognitions = select(func.count()).where(EntityRecognition.reprocessing_id == Reprocessing.id).scalar_subquery()
    statement = (
        select(
            Reprocessing.__table__,
            made_extractions.label("extractions_made"),
            made_recognitions.label("recognitions_made"),
            jobs(ACTIVE_JOB_STATUSES).label("open_jobs"),
            jobs([JobStatus.FAILED]).label("failed_jobs"),
        )
        .order_by(Reprocessing.created_at.desc(), Reprocessing.id.desc())
        .limit(limit)
    )
    return connection.execute(statement).all()


def replaced_in_events(connection: Connection, limit: int) -> Sequence[Row]:
    """Replaced extractions whose reports are still in events while their article's current reading has all of its
    reports in events: (the replaced extraction, its article, the current one)."""
    replaced = Extraction.__table__.alias("replaced")
    current = Extraction.__table__.alias("current")
    report = EventCandidate.__table__.alias("report")
    new_report = EventCandidate.__table__.alias("new_report")
    statement = (
        select(replaced.c.id.label("extraction_id"), replaced.c.article_id, current.c.id.label("current_id"))
        .join(
            current,
            and_(
                current.c.article_id == replaced.c.article_id,
                current.c.validation_result == ValidationResult.VALID,
                current.c.replaced_by_id.is_(None),
            ),
        )
        .where(
            replaced.c.replaced_by_id.is_not(None),
            exists().where(report.c.extraction_id == replaced.c.id, report.c.event_id.is_not(None)),
            ~exists().where(new_report.c.extraction_id == current.c.id, new_report.c.event_id.is_(None)),
        )
        .order_by(replaced.c.id)
        .limit(limit)
    )
    return connection.execute(statement).all()


def reports_in_events(connection: Connection, extraction_id: int) -> Sequence[Row]:
    """The extraction's reports that are in an event: id and event, in their order."""
    statement = (
        select(EventCandidate.id, EventCandidate.event_id)
        .where(EventCandidate.extraction_id == extraction_id, EventCandidate.event_id.is_not(None))
        .order_by(EventCandidate.position)
    )
    return connection.execute(statement).all()


def first_event(connection: Connection, extraction_id: int) -> int | None:
    """The event of the extraction's first report that has one."""
    statement = (
        select(EventCandidate.event_id)
        .where(EventCandidate.extraction_id == extraction_id, EventCandidate.event_id.is_not(None))
        .order_by(EventCandidate.position)
        .limit(1)
    )
    return connection.execute(statement).scalar_one_or_none()
