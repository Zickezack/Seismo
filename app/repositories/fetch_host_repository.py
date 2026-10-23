"""Data access for the fetch state per host: robots.txt and request slots. Times from the database clock."""
from datetime import datetime, timedelta

from sqlalchemy import Connection, Row, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import FetchHost

_ONE_SECOND = literal_column("interval '1 second'")


def get(connection: Connection, host: str, robots_max_age: timedelta) -> Row | None:
    """The host's robots.txt state, with `robots_fresh` telling whether it is younger than `robots_max_age`."""
    fresh = FetchHost.robots_fetched_at > func.now() - robots_max_age.total_seconds() * _ONE_SECOND
    statement = select(FetchHost.robots_status, FetchHost.robots_txt, fresh.label("robots_fresh")).where(FetchHost.host == host)
    return connection.execute(statement).one_or_none()


def save_robots(connection: Connection, host: str, status: str, robots_txt: str | None) -> None:
    values = {"robots_status": status, "robots_txt": robots_txt, "robots_fetched_at": func.now()}
    statement = insert(FetchHost).values(host=host, **values).on_conflict_do_update(index_elements=[FetchHost.host], set_=values)
    connection.execute(statement)


def reserve_slot(connection: Connection, host: str, interval_seconds: float) -> tuple[datetime, datetime]:
    """This caller's request slot and the current time.

    The slot starts at the host's next free slot or now, whichever is later; the next free slot moves one
    interval past it. The row lock serialises concurrent callers, so each gets a slot of its own.
    """
    connection.execute(insert(FetchHost).values(host=host).on_conflict_do_nothing(index_elements=[FetchHost.host]))
    slot = func.greatest(FetchHost.next_slot_at, func.now())
    statement = (
        update(FetchHost)
        .where(FetchHost.host == host)
        .values(next_slot_at=slot + interval_seconds * _ONE_SECOND)
        .returning(FetchHost.next_slot_at, func.now())
    )
    next_slot_at, now = connection.execute(statement).one()
    return next_slot_at - timedelta(seconds=interval_seconds), now


def lock_last_request(connection: Connection, host: str) -> tuple[datetime | None, datetime]:
    """When the last request to the host was sent, and the current time; the row stays locked."""
    statement = select(FetchHost.last_request_at, func.now()).where(FetchHost.host == host).with_for_update()
    row = connection.execute(statement).one_or_none()
    return (row[0], row[1]) if row else (None, connection.execute(select(func.now())).scalar_one())


def mark_request(connection: Connection, host: str) -> None:
    statement = insert(FetchHost).values(host=host, last_request_at=func.now())
    connection.execute(
        statement.on_conflict_do_update(index_elements=[FetchHost.host], set_={"last_request_at": func.now()})
    )
