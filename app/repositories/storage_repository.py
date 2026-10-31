"""Data access for the storage the data takes (Speicher): the database's and its tables' size, the raw texts', and the
daily measurements (Speichermessung)."""
from collections.abc import Sequence

from sqlalchemy import Connection, Row, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Article, RawText, Source, StorageMeasurement

# As stored - compressed (TOAST) -, the answer and its plain text.
_RAW_TEXT_BYTES = func.pg_column_size(RawText.content) + func.coalesce(func.pg_column_size(RawText.plain_text), 0)


def database_bytes(connection: Connection) -> int:
    return connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()


def table_bytes(connection: Connection) -> dict[str, int]:
    """Every table of the schema with its indexes and TOAST, the largest first; from the catalogue, nothing read."""
    statement = text("""
        SELECT c.relname, pg_total_relation_size(c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
        ORDER BY 2 DESC, 1
    """)
    return {name: size for name, size in connection.execute(statement)}


def article_count(connection: Connection) -> int:
    return connection.execute(select(func.count()).select_from(Article)).scalar_one()


def raw_texts(connection: Connection) -> tuple[int, int]:
    """How many raw texts there are, and what they take."""
    statement = select(func.count(), func.coalesce(func.sum(_RAW_TEXT_BYTES), 0)).select_from(RawText)
    count, size = connection.execute(statement).one()
    return count, int(size)


def raw_texts_by_source(connection: Connection) -> Sequence[Row]:
    """The raw texts per Quelle - a deleted one by the name its articles keep -, the largest first: id (None once
    deleted), name, how many, what they take."""
    name = func.coalesce(Source.name, Article.source_name)
    size = func.sum(_RAW_TEXT_BYTES)
    statement = (
        select(Source.id, name.label("name"), func.count().label("count"), size.label("size_bytes"))
        .select_from(RawText)
        .join(Article, Article.id == RawText.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .group_by(Source.id, name)
        .order_by(size.desc(), name)
    )
    return connection.execute(statement).all()


def measured_on(connection: Connection, day) -> bool:
    return connection.execute(select(StorageMeasurement.measured_on).where(StorageMeasurement.measured_on == day)).first() is not None


def insert_measurement(connection: Connection, values: dict) -> bool:
    """The day's measurement; False when it has one already."""
    statement = pg_insert(StorageMeasurement).values(values).on_conflict_do_nothing().returning(StorageMeasurement.measured_on)
    return connection.execute(statement).first() is not None


def measurements(connection: Connection, limit: int) -> Sequence[Row]:
    """The latest measurements, newest first."""
    statement = select(StorageMeasurement.__table__).order_by(StorageMeasurement.measured_on.desc()).limit(limit)
    return connection.execute(statement).all()
