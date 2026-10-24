"""Data access for the links between similar articles and the pairs the admin separated (article_link)."""
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, and_, exists, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Article, ArticleLink, Source


def get_for_links(connection: Connection, article_id: int) -> Row | None:
    statement = select(
        Article.id,
        Article.text_signature,
        Article.text_bands,
        func.coalesce(Article.published_at, Article.fetched_at).label("moment"),
        Article.canonical_url,
        Article.content_hash,
        Article.duplicate_of_id,
    ).where(Article.id == article_id)
    return connection.execute(statement).one_or_none()


def candidates(connection: Connection, article_id: int, bands: list[int], start: datetime, end: datetime) -> Sequence[Row]:
    """Originals sharing a band with the article, published (else fetched) from `start` to `end`, not yet paired with
    it - neither linked nor separated."""
    statement = select(Article.id, Article.text_signature, Article.canonical_url, Article.content_hash).where(
        Article.text_bands.overlap(bands),
        Article.id != article_id,
        Article.duplicate_of_id.is_(None),
        func.coalesce(Article.published_at, Article.fetched_at).between(start, end),
        ~exists().where(pair(article_id, Article.id)),
    )
    return connection.execute(statement).all()


def pair(first, second):
    """The condition for the row of a pair, whichever id is the lower."""
    return and_(
        ArticleLink.article_id == func.least(first, second), ArticleLink.other_id == func.greatest(first, second)
    )


def involving(article_id):
    """The condition for the rows of an article's pairs."""
    return or_(ArticleLink.article_id == article_id, ArticleLink.other_id == article_id)


def separated(first, second):
    return and_(pair(first, second), ArticleLink.separated_at.is_not(None))


def insert_link(connection: Connection, first: int, second: int, similarity: float) -> None:
    """Link the pair, unless it has a row already."""
    values = {"article_id": min(first, second), "other_id": max(first, second), "similarity": similarity}
    connection.execute(insert(ArticleLink).values(values).on_conflict_do_nothing())


def separate(connection: Connection, first: int, second: int) -> None:
    """The pair is unrelated from now on: its link, if any, is kept as separated."""
    values = {"article_id": min(first, second), "other_id": max(first, second), "separated_at": func.now()}
    statement = insert(ArticleLink).values(values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[ArticleLink.article_id, ArticleLink.other_id], set_={"separated_at": func.now()}
        )
    )


def lock_linked(connection: Connection, article_id: int, other_ids: list[int]) -> set[int]:
    """Which of the others are linked to the article now, their rows locked until the end of the transaction."""
    statement = (
        select(ArticleLink.article_id, ArticleLink.other_id)
        .where(or_(*(pair(article_id, other_id) for other_id in other_ids)), ArticleLink.separated_at.is_(None))
        .with_for_update()
    )
    return {row.other_id if row.article_id == article_id else row.article_id for row in connection.execute(statement)}


def move(connection: Connection, from_id: int, to_id: int) -> None:
    """The pairs of an article that became a duplicate go to its original, a separation winning over a link."""
    statement = text(
        """
        INSERT INTO article_link (article_id, other_id, similarity, created_at, separated_at)
        SELECT least(other, :to_id), greatest(other, :to_id), similarity, created_at, separated_at
        FROM (
            SELECT CASE WHEN article_id = :from_id THEN other_id ELSE article_id END AS other,
                similarity, created_at, separated_at
            FROM article_link WHERE :from_id IN (article_id, other_id)
        ) AS moved
        WHERE other <> :to_id
        ON CONFLICT (article_id, other_id) DO UPDATE
        SET separated_at = coalesce(article_link.separated_at, excluded.separated_at)
        """
    )
    connection.execute(statement, {"from_id": from_id, "to_id": to_id})
    connection.execute(ArticleLink.__table__.delete().where(involving(from_id)))


def linked(connection: Connection, article_id: int) -> Sequence[Row]:
    """The articles linked to this one, first published first, with their similarity and source's current name."""
    other_id = func.coalesce(
        func.nullif(ArticleLink.article_id, literal(article_id)), ArticleLink.other_id
    ).label("other_id")
    links = (
        select(other_id, ArticleLink.similarity)
        .where(involving(article_id), ArticleLink.separated_at.is_(None))
        .subquery()
    )
    statement = (
        select(
            Article.id,
            Article.title,
            Article.url,
            Article.published_at,
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
            links.c.similarity,
        )
        .join(links, links.c.other_id == Article.id)
        .outerjoin(Source, Source.id == Article.source_id)
        .order_by(Article.published_at.asc().nulls_last(), Article.id)
    )
    return connection.execute(statement).all()


def linked_ids(connection: Connection, article_id: int) -> set[int]:
    return {row.id for row in linked(connection, article_id)}
