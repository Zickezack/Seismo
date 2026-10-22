"""LLM slots as PostgreSQL advisory locks, held by the connection that took them.

Session-level advisory locks end with their connection, so a worker that crashes or is killed mid-call
never keeps a slot taken.
"""
from sqlalchemy import Connection, text

# First key of every slot lock; the second key is the slot number. Any constant unique to this purpose.
LLM_SLOT_LOCK_NAMESPACE = 7301


def try_acquire(connection: Connection, slot: int) -> bool:
    statement = text("SELECT pg_try_advisory_lock(CAST(:namespace AS integer), CAST(:slot AS integer))")
    return connection.execute(statement, {"namespace": LLM_SLOT_LOCK_NAMESPACE, "slot": slot}).scalar_one()


def release(connection: Connection, slot: int) -> bool:
    statement = text("SELECT pg_advisory_unlock(CAST(:namespace AS integer), CAST(:slot AS integer))")
    return connection.execute(statement, {"namespace": LLM_SLOT_LOCK_NAMESPACE, "slot": slot}).scalar_one()


def count_in_use(connection: Connection) -> int:
    """Slots held by any connection to this database. Two-key advisory locks show up with objsubid 2."""
    statement = text(
        "SELECT count(*) FROM pg_locks"
        " WHERE locktype = 'advisory' AND granted AND objsubid = 2 AND classid = CAST(:namespace AS oid)"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    return connection.execute(statement, {"namespace": LLM_SLOT_LOCK_NAMESPACE}).scalar_one()
