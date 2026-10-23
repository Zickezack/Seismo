"""Data access for the Quellen."""
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import Connection, Row, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import Source

# ICU collation: sorts "Äpfel" next to "Apfel" and ignores case first; plain lower() would put it after "Zitrone".
NAME_COLLATION = "de-x-icu"
UNIQUE_NAME_INDEX = "uq_source_name"


class NameTaken(Exception):
    """Another source already has this name, in any case."""


def insert_source(connection: Connection, values: dict) -> int:
    with _name_must_be_free(connection):
        return connection.execute(insert(Source).values(**values).returning(Source.id)).scalar_one()


def update_source(connection: Connection, source_id: int, values: dict) -> bool:
    """False when the source does not exist."""
    statement = update(Source).where(Source.id == source_id).values(**values, updated_at=func.now())
    with _name_must_be_free(connection):
        return connection.execute(statement).rowcount == 1


def get(connection: Connection, source_id: int) -> Row | None:
    return connection.execute(select(Source.__table__).where(Source.id == source_id)).one_or_none()


def all_by_name(connection: Connection) -> Sequence[Row]:
    statement = select(Source.__table__).order_by(Source.name.collate(NAME_COLLATION), Source.id)
    return connection.execute(statement).all()


def lock_sources(connection: Connection, source_ids: list[int]) -> Sequence[Row]:
    """Id and name of the existing ones, locked until the end of the transaction."""
    statement = select(Source.id, Source.name).where(Source.id.in_(source_ids)).order_by(Source.id).with_for_update()
    return connection.execute(statement).all()


def delete_sources(connection: Connection, source_ids: list[int]) -> int:
    return connection.execute(delete(Source).where(Source.id.in_(source_ids))).rowcount


def set_active(connection: Connection, source_ids: list[int], active: bool) -> int:
    statement = update(Source).where(Source.id.in_(source_ids)).values(is_active=active, updated_at=func.now())
    return connection.execute(statement).rowcount


@contextmanager
def _name_must_be_free(connection: Connection) -> Iterator[None]:
    # The savepoint keeps the caller's transaction usable after the unique violation.
    try:
        with connection.begin_nested():
            yield
    except IntegrityError as error:
        if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == UNIQUE_NAME_INDEX:
            raise NameTaken from None
        raise
