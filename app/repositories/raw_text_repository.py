"""Data access for the Rohtexte: article content as fetched, in the temporary raw-data layer, and when it is due for
deletion (raw_text_service)."""
from sqlalchemy import Connection, and_, case, delete, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Article, Extraction, NormalizationStatus, RawText, Source, ValidationResult
from app.repositories.extraction_repository import CURRENT

_DAY = literal_column("interval '1 day'")
_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)


def insert_raw_text(connection: Connection, article_id: int, content: bytes, content_type: str | None, url: str) -> bool:
    """False when the article has its raw text already."""
    statement = (
        insert(RawText)
        .values(article_id=article_id, content=content, content_type=content_type, url=url)
        .on_conflict_do_nothing(index_elements=[RawText.article_id])
    )
    return connection.execute(statement).rowcount == 1


def _lifecycle(extraction_max_age_days: int, default_retention_days: int):
    """Per raw text: its article, when its processing ended (NULL while it goes on or failed), whether it failed, and
    its source's retention period - the default once the source is deleted.

    Ended with the article's current extraction; for a duplicate, never extracted, with its normalization; for any
    other article once the extraction can take it no more - its time older than the extraction's maximum age. Failed:
    a text no reader understood, or only unusable answers of the model once that time is over."""
    # Per article, once: when its current extraction was made, and whether the model gave an unusable answer.
    runs = (
        select(
            Extraction.article_id,
            func.max(Extraction.extracted_at).filter(CURRENT).label("extracted_at"),
            func.bool_or(Extraction.validation_result != ValidationResult.VALID).label("unusable"),
        )
        .group_by(Extraction.article_id)
        .subquery()
    )
    extracted_at, unusable = runs.c.extracted_at, func.coalesce(runs.c.unusable, False)
    window_end = _MOMENT + extraction_max_age_days * _DAY
    window_over = window_end <= func.now()
    done = Article.normalization_status == NormalizationStatus.DONE
    processed_at = case(
        (~done, None),
        (extracted_at.is_not(None), func.greatest(Article.normalized_at, extracted_at)),
        (Article.duplicate_of_id.is_not(None), Article.normalized_at),
        (unusable, None),
        (window_over, func.greatest(Article.normalized_at, window_end)),
        else_=None,
    )
    failed = or_(
        Article.normalization_status == NormalizationStatus.FAILED,
        and_(done, extracted_at.is_(None), Article.duplicate_of_id.is_(None), unusable, window_over),
    )
    retention = func.coalesce(Source.raw_text_retention_days, default_retention_days)
    return (
        select(
            RawText.article_id,
            processed_at.label("processed_at"),
            failed.label("failed"),
            retention.label("retention_days"),
        )
        .join(Article, Article.id == RawText.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(runs, runs.c.article_id == Article.id)
        .subquery()
    )


def delete_due(connection: Connection, extraction_max_age_days: int, default_retention_days: int, limit: int) -> list[int]:
    """Delete up to `limit` raw texts whose retention period is over since their processing ended, and mark their
    articles; the articles' ids."""
    lifecycle = _lifecycle(extraction_max_age_days, default_retention_days)
    due = (
        select(lifecycle.c.article_id)
        .where(lifecycle.c.processed_at + lifecycle.c.retention_days * _DAY <= func.now())
        .order_by(lifecycle.c.article_id)
        .limit(limit)
    )
    deleted = list(connection.execute(
        delete(RawText).where(RawText.article_id.in_(due.scalar_subquery())).returning(RawText.article_id)
    ).scalars())
    if deleted:
        connection.execute(update(Article).where(Article.id.in_(deleted)).values(raw_text_deleted_at=func.now()))
    return sorted(deleted)


def lifecycle_counts(connection: Connection, extraction_max_age_days: int, default_retention_days: int) -> dict[str, int]:
    """The raw texts by where they stand - in processing, kept for their retention period, due, kept because their
    processing failed - and the ones deleted so far."""
    lifecycle = _lifecycle(extraction_max_age_days, default_retention_days)
    due = lifecycle.c.processed_at + lifecycle.c.retention_days * _DAY <= func.now()
    counts = select(
        func.count().filter(lifecycle.c.failed).label("failed"),
        func.count().filter(~lifecycle.c.failed, lifecycle.c.processed_at.is_(None)).label("processing"),
        func.count().filter(~lifecycle.c.failed, lifecycle.c.processed_at.is_not(None), ~due).label("kept"),
        func.count().filter(~lifecycle.c.failed, due).label("due"),
    ).select_from(lifecycle)
    deleted = select(func.count()).select_from(Article).where(Article.raw_text_deleted_at.is_not(None))
    return dict(connection.execute(counts).one()._mapping) | {"deleted": connection.execute(deleted).scalar_one()}
