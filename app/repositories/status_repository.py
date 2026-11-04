"""Data access for the status page (status_service): the LLM calls, the job kinds' hourly values, the queue now, and
the articles' throughput, failures and latencies over a period - read from the tables that keep them."""
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, and_, case, extract, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import (
    Article,
    EventCandidate,
    Extraction,
    FullTextStatus,
    Job,
    JobHour,
    JobStatus,
    LlmCall,
    NormalizationStatus,
    Source,
    ValidationResult,
)
from app.repositories.source_repository import NAME_COLLATION


def _seconds(interval):
    return extract("epoch", interval)


def insert_llm_call(connection: Connection, values: dict) -> None:
    connection.execute(insert(LlmCall).values(values))


def article_of_extraction(connection: Connection, extraction_id: int) -> int | None:
    return connection.execute(select(Extraction.article_id).where(Extraction.id == extraction_id)).scalar_one_or_none()


def article_of_candidate(connection: Connection, candidate_id: int) -> int | None:
    return connection.execute(select(EventCandidate.article_id).where(EventCandidate.id == candidate_id)).scalar_one_or_none()


# The job kinds' hours


def job_stats(connection: Connection, start: datetime, end: datetime) -> dict[str, dict]:
    """Per kind, of the jobs finished from `start` to `end`: how many, failed for good, their attempts and the failed
    ones among them, their run times; of those started then, their waits from when they were due."""
    failed = Job.status == JobStatus.FAILED
    finished = (
        select(
            Job.kind,
            func.count().label("finished"),
            func.count().filter(failed).label("failed"),
            func.coalesce(func.sum(Job.attempts), 0).label("attempts"),
            func.coalesce(func.sum(case((failed, Job.attempts), else_=Job.attempts - 1)), 0).label("failed_attempts"),
            func.coalesce(func.sum(_seconds(Job.finished_at - Job.started_at)), 0).label("run_seconds"),
            func.coalesce(func.max(_seconds(Job.finished_at - Job.started_at)), 0).label("run_seconds_max"),
        )
        .where(Job.finished_at >= start, Job.finished_at < end, Job.started_at.isnot(None))
        .group_by(Job.kind)
    )
    wait = func.greatest(_seconds(Job.started_at - Job.run_after), 0)
    started = (
        select(
            Job.kind, func.count().label("started"), func.coalesce(func.sum(wait), 0).label("wait_seconds"),
            func.coalesce(func.max(wait), 0).label("wait_seconds_max"),
        )
        .where(Job.started_at >= start, Job.started_at < end)
        .group_by(Job.kind)
    )
    stats: dict[str, dict] = {}
    for row in connection.execute(finished):
        stats[row.kind] = {key: row._mapping[key] for key in row._mapping if key != "kind"}
    for row in connection.execute(started):
        stats.setdefault(row.kind, {}).update({key: row._mapping[key] for key in row._mapping if key != "kind"})
    return stats


def queue(connection: Connection, now: datetime) -> Sequence[Row]:
    """The queue now, per kind and priority: due and waiting, waiting for later, running, and how long the oldest due
    one has waited (seconds)."""
    queued = Job.status == JobStatus.QUEUED
    due = and_(queued, Job.run_after <= now)
    statement = (
        select(
            Job.kind, Job.priority,
            func.count().filter(due).label("due"),
            func.count().filter(queued, Job.run_after > now).label("later"),
            func.count().filter(Job.status == JobStatus.RUNNING).label("running"),
            func.max(_seconds(literal(now) - Job.run_after)).filter(due).label("oldest_due_seconds"),
        )
        .where(Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
        .group_by(Job.kind, Job.priority)
        .order_by(Job.kind, Job.priority)
    )
    return connection.execute(statement).all()


def insert_job_hours(connection: Connection, rows: list[dict]) -> int:
    """Each hour and kind once: a second worker's is left out."""
    if not rows:
        return 0
    statement = insert(JobHour).values(rows).on_conflict_do_nothing(index_elements=[JobHour.hour, JobHour.kind]).returning(JobHour.hour)
    return len(connection.execute(statement).all())


def latest_hour(connection: Connection) -> datetime | None:
    return connection.execute(select(func.max(JobHour.hour))).scalar_one()


def job_hours(connection: Connection, start: datetime, end: datetime) -> Sequence[Row]:
    statement = select(JobHour).where(JobHour.hour >= start, JobHour.hour < end).order_by(JobHour.hour, JobHour.kind)
    return connection.execute(statement).all()


# LLM calls


def llm_by_kind(connection: Connection, start: datetime, end: datetime) -> Sequence[Row]:
    """Per kind: calls, their seconds and tokens, and how many brought no usable answer."""
    statement = (
        select(
            LlmCall.kind, func.count().label("calls"), func.sum(LlmCall.duration_ms).label("ms"),
            func.sum(LlmCall.prompt_tokens).label("prompt_tokens"), func.sum(LlmCall.answer_tokens).label("answer_tokens"),
            func.count().filter(LlmCall.outcome != "answered").label("unanswered"),
            func.count(func.distinct(LlmCall.article_id)).label("articles"),
        )
        .where(LlmCall.called_at >= start, LlmCall.called_at < end)
        .group_by(LlmCall.kind)
        .order_by(LlmCall.kind)
    )
    return connection.execute(statement).all()


def llm_articles(connection: Connection, start: datetime, end: datetime, kinds) -> int:
    """How many articles the calls of these kinds in the period worked for."""
    statement = select(func.count(func.distinct(LlmCall.article_id))).where(
        LlmCall.called_at >= start, LlmCall.called_at < end, LlmCall.kind.in_(list(kinds)), LlmCall.article_id.isnot(None),
    )
    return connection.execute(statement).scalar_one()


def llm_by_bucket(connection: Connection, start: datetime, end: datetime, unit: str) -> Sequence[Row]:
    bucket = func.date_trunc(literal(unit, literal_execute=True), LlmCall.called_at, literal("UTC", literal_execute=True))
    statement = (
        select(bucket.label("bucket"), func.count().label("calls"), func.sum(LlmCall.duration_ms).label("ms"))
        .where(LlmCall.called_at >= start, LlmCall.called_at < end)
        .group_by(bucket)
    )
    return connection.execute(statement).all()


# Articles


def _failed():
    """An article whose processing failed at a step: its full text, its normalization, or its extraction's answer."""
    unusable = (
        select(Extraction.id)
        .where(Extraction.article_id == Article.id, Extraction.validation_result != ValidationResult.VALID)
        .exists()
    )
    return or_(
        Article.full_text_status == FullTextStatus.FAILED, Article.normalization_status == NormalizationStatus.FAILED, unusable
    )


def _with_event():
    return select(EventCandidate.id).where(EventCandidate.article_id == Article.id, EventCandidate.event_id.isnot(None)).exists()


def articles(connection: Connection, start: datetime, end: datetime) -> Row:
    """The articles fetched in the period: live and by a backfill, duplicates, failed, extracted, with an event."""
    extracted = select(Extraction.id).where(Extraction.article_id == Article.id).exists()
    statement = (
        select(
            func.count().filter(Article.backfill_id.is_(None)).label("live"),
            func.count().filter(Article.backfill_id.isnot(None)).label("backfill"),
            func.count().filter(Article.duplicate_of_id.isnot(None)).label("duplicates"),
            func.count().filter(_failed()).label("failed"),
            func.count().filter(extracted).label("extracted"),
            func.count().filter(_with_event()).label("with_event"),
        )
        .where(Article.fetched_at >= start, Article.fetched_at < end)
    )
    return connection.execute(statement).one()


def extracted_articles(connection: Connection, start: datetime, end: datetime) -> int:
    """Articles whose first extraction run was in the period: what the LLM's article steps worked for."""
    first = select(Extraction.article_id, func.min(Extraction.extracted_at).label("at")).group_by(Extraction.article_id).subquery()
    return connection.execute(select(func.count()).select_from(first).where(first.c.at >= start, first.c.at < end)).scalar_one()


def by_source(connection: Connection, start: datetime, end: datetime) -> Sequence[Row]:
    """Per source, of the articles fetched in the period: how many, duplicates, failed, with an event, and the LLM
    milliseconds spent on them (whenever); by name."""
    llm = (
        select(LlmCall.article_id, func.sum(LlmCall.duration_ms).label("ms"))
        .where(LlmCall.article_id.isnot(None))
        .group_by(LlmCall.article_id)
        .subquery()
    )
    statement = (
        select(
            Article.source_id, func.coalesce(Source.name, func.max(Article.source_name)).label("name"),
            func.count().label("articles"),
            func.count().filter(Article.duplicate_of_id.isnot(None)).label("duplicates"),
            func.count().filter(_failed()).label("failed"),
            func.count().filter(_with_event()).label("with_event"),
            func.count(llm.c.article_id).label("llm_articles"),
            func.coalesce(func.sum(llm.c.ms), 0).label("llm_ms"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(llm, llm.c.article_id == Article.id)
        .where(Article.fetched_at >= start, Article.fetched_at < end)
        .group_by(Article.source_id, Source.name)
        .order_by(func.coalesce(Source.name, func.max(Article.source_name)).collate(NAME_COLLATION), Article.source_id)
    )
    return connection.execute(statement).all()


def articles_by_bucket(connection: Connection, start: datetime, end: datetime, unit: str) -> Sequence[Row]:
    bucket = func.date_trunc(literal(unit, literal_execute=True), Article.fetched_at, literal("UTC", literal_execute=True))
    statement = (
        select(bucket.label("bucket"), func.count().label("articles"), func.count().filter(_failed()).label("failed"))
        .where(Article.fetched_at >= start, Article.fetched_at < end)
        .group_by(bucket)
    )
    return connection.execute(statement).all()


def latencies(connection: Connection, start: datetime, end: datetime) -> dict[str, Row]:
    """Of the live articles (no backfill's), the median and 90th percentile in seconds of each step that ended in the
    period: fetched to normalized, normalized to first extracted, first extracted to first assigned to an event, and
    fetched to assigned."""
    first_extraction = (
        select(Extraction.article_id, func.min(Extraction.extracted_at).label("at")).group_by(Extraction.article_id).subquery()
    )
    first_assignment = (
        select(EventCandidate.article_id, func.min(EventCandidate.assigned_at).label("at"))
        .where(EventCandidate.assigned_at.isnot(None))
        .group_by(EventCandidate.article_id)
        .subquery()
    )
    live = Article.backfill_id.is_(None)
    steps = {
        "normalized": (Article.normalized_at - Article.fetched_at, Article.normalized_at, None),
        "extracted": (first_extraction.c.at - Article.normalized_at, first_extraction.c.at, first_extraction),
        "assigned": (first_assignment.c.at - first_extraction.c.at, first_assignment.c.at, "both"),
        "total": (first_assignment.c.at - Article.fetched_at, first_assignment.c.at, first_assignment),
    }
    found = {}
    for key, (span, ended, joined) in steps.items():
        seconds = func.greatest(_seconds(span), 0)
        statement = select(
            func.count().label("count"),
            func.percentile_cont(0.5).within_group(seconds).label("median"),
            func.percentile_cont(0.9).within_group(seconds).label("p90"),
        ).select_from(Article)
        if joined is first_extraction or joined == "both":
            statement = statement.join(first_extraction, first_extraction.c.article_id == Article.id)
        if joined is first_assignment or joined == "both":
            statement = statement.join(first_assignment, first_assignment.c.article_id == Article.id)
        statement = statement.where(live, ended >= start, ended < end, span.isnot(None))
        found[key] = connection.execute(statement).one()
    return found
