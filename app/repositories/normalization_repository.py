"""Data access for normalizing articles: which are due, what they hold, the result."""
from collections.abc import Sequence

from sqlalchemy import Connection, Row, exists, func, literal, select, update

from app.db.models import ACTIVE_JOB_STATUSES, Article, FullTextStatus, Job, NormalizationStatus, RawText, Source


def due(connection: Connection, job_kind: str, limit: int) -> Sequence[Row]:
    """Articles whose full text is settled, not normalized yet and without a waiting or running job: id and backfill."""
    active_job = exists().where(
        Job.dedupe_key == func.concat(literal(f"{job_kind}:"), Article.id),
        Job.status.in_(ACTIVE_JOB_STATUSES),
    )
    statement = (
        select(Article.id, Article.backfill_id)
        .where(
            Article.normalization_status == NormalizationStatus.PENDING,
            Article.full_text_status != FullTextStatus.PENDING,
            ~active_job,
        )
        .order_by(Article.id)
        .limit(limit)
    )
    return connection.execute(statement).all()


def get(connection: Connection, article_id: int) -> Row | None:
    """The article's links, title and time, with its raw text while it is kept (content NULL without one) and its
    source's type (NULL once the source is deleted)."""
    statement = (
        select(
            Article.id,
            Article.url,
            Article.canonical_url,
            Article.title,
            Article.published_at,
            Article.published_at_source,
            Article.normalization_status,
            RawText.content,
            RawText.content_type,
            RawText.url.label("raw_url"),
            Source.adapter_type,
        )
        .outerjoin(RawText, RawText.article_id == Article.id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(Article.id == article_id)
    )
    return connection.execute(statement).one_or_none()


def finish(connection: Connection, article_id: int, values: dict, plain_text: str | None) -> bool:
    """Write the result once, while the article still waits for it; the cleaned text goes to its raw text."""
    statement = (
        update(Article)
        .where(Article.id == article_id, Article.normalization_status == NormalizationStatus.PENDING)
        .values(normalized_at=func.now(), **values)
    )
    if connection.execute(statement).rowcount != 1:
        return False
    if plain_text is not None:
        connection.execute(update(RawText).where(RawText.article_id == article_id).values(plain_text=plain_text))
    return True
