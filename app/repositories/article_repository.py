"""Data access for the Artikel."""
from collections.abc import Sequence

from sqlalchemy import Connection, Row, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Article, Source


def insert_new(connection: Connection, rows: list[dict]) -> int:
    """Insert the rows whose (source_id, item_key) is not stored yet; the number inserted."""
    if not rows:
        return 0
    statement = insert(Article).values(rows).on_conflict_do_nothing(index_elements=[Article.source_id, Article.item_key])
    return connection.execute(statement).rowcount


def latest(connection: Connection, source_id: int | None, limit: int) -> Sequence[Row]:
    """The most recently fetched articles, with the source's current name while it exists."""
    statement = (
        select(
            Article.__table__,
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
            (Source.id.is_not(None)).label("source_exists"),
        )
        .outerjoin(Source, Source.id == Article.source_id)
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


def count_by_source(connection: Connection) -> dict[int, int]:
    statement = select(Article.source_id, func.count()).where(Article.source_id.is_not(None)).group_by(Article.source_id)
    return dict(connection.execute(statement).all())
