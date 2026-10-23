"""Data access for the Rohtexte: article content as fetched, in the temporary raw-data layer."""
from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from app.db.models import RawText


def insert_raw_text(connection: Connection, article_id: int, content: bytes, content_type: str | None, url: str) -> bool:
    """False when the article has its raw text already."""
    statement = (
        insert(RawText)
        .values(article_id=article_id, content=content, content_type=content_type, url=url)
        .on_conflict_do_nothing(index_elements=[RawText.article_id])
    )
    return connection.execute(statement).rowcount == 1
