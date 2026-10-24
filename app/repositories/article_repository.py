"""Data access for the Artikel."""
from collections.abc import Sequence

from sqlalchemy import Connection, Row, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Article, FullTextStatus, RawText, Source


def insert_new(connection: Connection, rows: list[dict]) -> list[int]:
    """Insert the rows whose (source_id, item_key) is not stored yet; the ids of the inserted ones."""
    if not rows:
        return []
    statement = (
        insert(Article)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Article.source_id, Article.item_key])
        .returning(Article.id)
    )
    return sorted(connection.execute(statement).scalars())


def get_for_full_text(connection: Connection, article_id: int) -> Row | None:
    """URL and full-text status of the article, whether its source still exists and is active, and the source's
    own minimum interval between requests."""
    statement = (
        select(
            Article.id,
            Article.url,
            Article.canonical_url,
            Article.full_text_status,
            Source.is_active.label("source_active"),
            Source.min_request_interval_seconds.label("source_min_request_interval_seconds"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
        .where(Article.id == article_id)
    )
    return connection.execute(statement).one_or_none()


def finish_full_text(connection: Connection, article_id: int, status: FullTextStatus, error: str | None) -> bool:
    """Record the outcome, once: False when the article is gone or its full text was settled before."""
    statement = (
        update(Article)
        .where(Article.id == article_id, Article.full_text_status == FullTextStatus.PENDING)
        .values(full_text_status=status, full_text_error=error)
    )
    return connection.execute(statement).rowcount == 1


def latest(connection: Connection, source_id: int | None, limit: int) -> Sequence[Row]:
    """The most recently fetched articles, with the source's current name while it exists."""
    statement = (
        select(
            Article.__table__,
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
            (Source.id.is_not(None)).label("source_exists"),
            func.octet_length(RawText.content).label("full_text_bytes"),
            func.length(RawText.plain_text).label("text_chars"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
        .outerjoin(RawText, RawText.article_id == Article.id)
        .order_by(Article.fetched_at.desc(), Article.published_at.desc().nulls_last(), Article.id.desc())
        .limit(limit)
    )
    if source_id is not None:
        statement = statement.where(Article.source_id == source_id)
    return connection.execute(statement).all()


def count(connection: Connection, source_id: int | None) -> int:
    statement = select(func.count()).select_from(Article)
    if source_id is not None:
        statement = statement.where(Article.source_id == source_id)
    return connection.execute(statement).scalar_one()


def count_by_source(connection: Connection, full_text_status: FullTextStatus | None = None) -> dict[int, int]:
    """Articles per existing source, all of them or those with this full-text status."""
    statement = select(Article.source_id, func.count()).where(Article.source_id.is_not(None)).group_by(Article.source_id)
    if full_text_status is not None:
        statement = statement.where(Article.full_text_status == full_text_status)
    return dict(connection.execute(statement).all())


def reset_blocked(connection: Connection, source_ids: list[int]) -> Sequence[Row]:
    """The blocked full texts of these sources back to pending; id and backfill of each article."""
    statement = (
        update(Article)
        .where(Article.source_id.in_(source_ids), Article.full_text_status == FullTextStatus.BLOCKED)
        .values(full_text_status=FullTextStatus.PENDING, full_text_error=None)
        .returning(Article.id, Article.backfill_id)
    )
    return connection.execute(statement).all()
