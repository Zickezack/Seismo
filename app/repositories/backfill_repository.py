"""Data access for the Backfills of the Quellen. Times from the database clock."""
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, func, insert, select, update

from app.db.models import Article, Backfill, BackfillStatus, FullTextStatus


def insert_backfill(connection: Connection, source_id: int, period_start: datetime, period_end: datetime) -> int:
    """A running backfill whose first request ends at the end of its period."""
    statement = (
        insert(Backfill)
        .values(source_id=source_id, period_start=period_start, period_end=period_end, cursor=period_end)
        .returning(Backfill.id)
    )
    return connection.execute(statement).scalar_one()


def get(connection: Connection, backfill_id: int) -> Row | None:
    return connection.execute(select(Backfill.__table__).where(Backfill.id == backfill_id)).one_or_none()


def lock_running(connection: Connection, backfill_id: int) -> bool:
    """Lock the backfill for the rest of the transaction, if it is still running."""
    statement = select(Backfill.id).where(Backfill.id == backfill_id, Backfill.status == BackfillStatus.RUNNING)
    return connection.execute(statement.with_for_update()).first() is not None


def record_page(connection: Connection, backfill_id: int, found: int, new: int, cursor: datetime | None) -> None:
    """Count a fetched page; the next request ends at `cursor`, or the backfill is done when it is None."""
    values = {"articles_found": Backfill.articles_found + found, "articles_new": Backfill.articles_new + new}
    if cursor is None:
        values |= {"cursor": Backfill.period_start, "status": BackfillStatus.DONE, "finished_at": func.now()}
    else:
        values["cursor"] = cursor
    connection.execute(update(Backfill).where(Backfill.id == backfill_id).values(**values))


def finish(connection: Connection, backfill_ids: list[int], status: BackfillStatus, error: str | None) -> int:
    """End running backfills as failed or cancelled; one that ended before stays as it is."""
    statement = (
        update(Backfill)
        .where(Backfill.id.in_(backfill_ids), Backfill.status == BackfillStatus.RUNNING)
        .values(status=status, last_error=error, finished_at=func.now())
    )
    return connection.execute(statement).rowcount


def lock_backfills(connection: Connection, backfill_ids: list[int]) -> Sequence[Row]:
    """Id, source and status of the existing ones, locked until the end of the transaction."""
    statement = (
        select(Backfill.id, Backfill.source_id, Backfill.status)
        .where(Backfill.id.in_(backfill_ids))
        .order_by(Backfill.id)
        .with_for_update()
    )
    return connection.execute(statement).all()


def for_source(connection: Connection, source_id: int) -> Sequence[Row]:
    """The source's backfills, newest first, with how many of their new articles' full texts are stored or pending."""

    def full_texts(status: FullTextStatus):
        return (
            select(func.count())
            .where(Article.backfill_id == Backfill.id, Article.full_text_status == status)
            .scalar_subquery()
        )

    statement = (
        select(
            Backfill.__table__,
            full_texts(FullTextStatus.STORED).label("full_texts_stored"),
            full_texts(FullTextStatus.PENDING).label("full_texts_pending"),
        )
        .where(Backfill.source_id == source_id)
        .order_by(Backfill.created_at.desc(), Backfill.id.desc())
    )
    return connection.execute(statement).all()
