"""Data access for the seed log: which seed items were imported once (architecture.md, "Seed Data")."""
from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import SeedLogEntry


def record(connection: Connection, seed_source: str, item_key: str) -> bool:
    """Log the item as imported; False when it was imported before.

    A concurrent run importing the same item waits on the primary key for this one's commit and then gets False.
    """
    statement = (
        insert(SeedLogEntry)
        .values(seed_source=seed_source, item_key=item_key)
        .on_conflict_do_nothing()
        .returning(SeedLogEntry.item_key)
    )
    return connection.execute(statement).first() is not None


def forget(connection: Connection, seed_source: str, item_key: str) -> None:
    statement = delete(SeedLogEntry).where(SeedLogEntry.seed_source == seed_source, SeedLogEntry.item_key == item_key)
    connection.execute(statement)


def imported_keys(connection: Connection, seed_source: str) -> set[str]:
    statement = select(SeedLogEntry.item_key).where(SeedLogEntry.seed_source == seed_source)
    return set(connection.execute(statement).scalars())
