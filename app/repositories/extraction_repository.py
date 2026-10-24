"""Data access for the event extraction: which articles are due, what an extraction reads and writes, and what the
article page shows of it."""
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import Connection, Row, and_, case, exists, func, insert, literal, select, tuple_
from sqlalchemy.orm import aliased

from app.db.models import (
    Article,
    ArticleLink,
    EventCandidate,
    Evidence,
    Extraction,
    Job,
    NormalizationStatus,
    RawText,
    Source,
    Statement,
    ValidationResult,
)
from app.repositories.article_link_repository import involving

_INFINITY = literal("infinity").cast(Article.published_at.type)


def _valid(article_id):
    """An extraction of the article with a valid answer: a run with an unusable one is no extraction."""
    return exists().where(Extraction.article_id == article_id, Extraction.validation_result == ValidationResult.VALID)


def due(
    connection: Connection,
    job_kind: str,
    languages: list[str],
    min_text_chars: int,
    max_age_days: int,
    limit: int,
) -> list[int]:
    """Articles that pass the pre-filter (extraction_service), newest first: originals of a source that extracts, in a
    listed language, with enough text, not too old, never extracted or queued before, and no later copy of a linked
    article."""
    moment = func.coalesce(Article.published_at, Article.fetched_at)
    other = aliased(Article)
    other_id = case((ArticleLink.article_id == Article.id, ArticleLink.other_id), else_=ArticleLink.article_id)
    earlier_copy = (
        select(literal(1))
        .select_from(ArticleLink)
        .join(other, other.id == other_id)
        .where(
            involving(Article.id),
            ArticleLink.separated_at.is_(None),
            tuple_(func.coalesce(other.published_at, _INFINITY), other.id)
            < tuple_(func.coalesce(Article.published_at, _INFINITY), Article.id),
        )
        .exists()
    )
    statement = (
        select(Article.id)
        .join(Source, and_(Source.id == Article.source_id, Source.extract_events))
        .join(RawText, RawText.article_id == Article.id)
        .where(
            Article.duplicate_of_id.is_(None),
            Article.normalization_status == NormalizationStatus.DONE,
            func.length(RawText.plain_text) >= max(min_text_chars, 1),
            func.coalesce(Article.detected_language, Article.language).in_(languages),
            moment >= func.now() - timedelta(days=max_age_days),
            ~_valid(Article.id),
            # Any job of it, failed ones too: a failed extraction comes back only when the admin requeues it.
            ~exists().where(Job.dedupe_key == func.concat(literal(f"{job_kind}:"), Article.id)),
            ~earlier_copy,
        )
        .order_by(moment.desc(), Article.id.desc())
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def jobs_since(connection: Connection, job_kind: str, period: timedelta) -> int:
    """Jobs of the kind queued within the period, whatever became of them: the budget's count."""
    statement = select(func.count()).select_from(Job).where(Job.kind == job_kind, Job.created_at >= func.now() - period)
    return connection.execute(statement).scalar_one()


def get_for_extraction(connection: Connection, article_id: int) -> Row | None:
    """What the model reads of an article, and whether it is still an original without an extraction; its link and its
    source's quality, for the quality of its quotes."""
    statement = (
        select(
            Article.id,
            Article.title,
            Article.published_at,
            Article.duplicate_of_id,
            func.coalesce(Article.canonical_url, Article.url).label("url"),
            func.coalesce(Source.name, Article.source_name).label("source_name"),
            Source.quality.label("source_quality"),
            RawText.plain_text,
            _valid(Article.id).label("extracted"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(RawText, RawText.article_id == Article.id)
        .where(Article.id == article_id)
    )
    return connection.execute(statement).one_or_none()


def insert_run(connection: Connection, values: dict) -> int:
    """A run whose answer was not usable, with its validation result and errors."""
    return connection.execute(insert(Extraction).values(values).returning(Extraction.id)).scalar_one()


def insert_extraction(connection: Connection, values: dict, candidates: list[dict]) -> int | None:
    """The extraction with its candidates, their statements and quotes; None when the article has one already."""
    if connection.execute(select(_valid(values["article_id"]))).scalar_one():
        return None
    extraction_id = connection.execute(insert(Extraction).values(values).returning(Extraction.id)).scalar_one()
    for position, candidate in enumerate(candidates):
        statements = candidate.pop("statements")
        row = candidate | {"extraction_id": extraction_id, "article_id": values["article_id"], "position": position}
        candidate_id = connection.execute(insert(EventCandidate).values(row).returning(EventCandidate.id)).scalar_one()
        for statement_position, statement in enumerate(statements):
            statement_values = {
                "candidate_id": candidate_id, "position": statement_position, "kind": statement["kind"],
                "text": statement["text"], "extraction_confidence": statement["confidence"],
            }
            statement_id = connection.execute(insert(Statement).values(statement_values).returning(Statement.id)).scalar_one()
            evidence_values = {
                "statement_id": statement_id, "article_id": values["article_id"], "quote": statement["quote"],
                "source_quality": statement["source_quality"],
            }
            connection.execute(insert(Evidence).values(evidence_values))
    return extraction_id


def latest(connection: Connection, article_id: int) -> Row | None:
    """The article's latest extraction with a valid answer."""
    statement = (
        select(Extraction.__table__)
        .where(Extraction.article_id == article_id, Extraction.validation_result == ValidationResult.VALID)
        .order_by(Extraction.extracted_at.desc(), Extraction.id.desc())
        .limit(1)
    )
    return connection.execute(statement).one_or_none()


def unusable_runs(connection: Connection, article_id: int) -> Sequence[Row]:
    """The article's runs whose answer was not usable, the latest first."""
    statement = (
        select(Extraction.__table__)
        .where(Extraction.article_id == article_id, Extraction.validation_result != ValidationResult.VALID)
        .order_by(Extraction.extracted_at.desc(), Extraction.id.desc())
    )
    return connection.execute(statement).all()


def candidates_of(connection: Connection, extraction_id: int) -> Sequence[Row]:
    statement = (
        select(EventCandidate.__table__).where(EventCandidate.extraction_id == extraction_id).order_by(EventCandidate.position)
    )
    return connection.execute(statement).all()


def statements_of(connection: Connection, extraction_id: int) -> Sequence[Row]:
    """The statements of an extraction's candidates with their quotes, in order."""
    statement = (
        select(
            Statement.candidate_id, Statement.kind, Statement.text, Statement.extraction_confidence, Evidence.quote,
            Evidence.source_quality,
        )
        .join(EventCandidate, EventCandidate.id == Statement.candidate_id)
        .join(Evidence, Evidence.statement_id == Statement.id)
        .where(EventCandidate.extraction_id == extraction_id)
        .order_by(EventCandidate.position, Statement.position)
    )
    return connection.execute(statement).all()
