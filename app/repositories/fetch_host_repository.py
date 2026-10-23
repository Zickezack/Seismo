"""Data access for the fetch state per host: robots.txt, request slots and pauses. Times from the database clock."""
from datetime import datetime, timedelta

from sqlalchemy import Connection, Row, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import FetchHost

_ONE_SECOND = literal_column("interval '1 second'")


def get(connection: Connection, host: str, robots_max_age: timedelta) -> Row | None:
    """The host's robots.txt state, with `robots_fresh` telling whether it is younger than `robots_max_age`,
    and `paused` whether the host's pause (Retry-After) still lasts."""
    fresh = FetchHost.robots_fetched_at > func.now() - robots_max_age.total_seconds() * _ONE_SECOND
    paused = func.coalesce(FetchHost.paused_until > func.now(), False)
    statement = select(
        FetchHost.robots_status, FetchHost.robots_txt, fresh.label("robots_fresh"), paused.label("paused")
    ).where(FetchHost.host == host)
    return connection.execute(statement).one_or_none()


def save_robots(connection: Connection, host: str, status: str, robots_txt: str | None) -> None:
    values = {"robots_status": status, "robots_txt": robots_txt, "robots_fetched_at": func.now()}
    statement = insert(FetchHost).values(host=host, **values).on_conflict_do_update(index_elements=[FetchHost.host], set_=values)
    connection.execute(statement)


def reserve_slot(connection: Connection, host: str, interval_seconds: float) -> tuple[datetime, datetime]:
    """This caller's request slot and the current time.

    The slot starts at the host's next free slot, the end of its pause or now, whichever is latest; the next
    free slot moves one interval past it. The row lock serialises concurrent callers, so each gets a slot of
    its own.
    """
    connection.execute(insert(FetchHost).values(host=host).on_conflict_do_nothing(index_elements=[FetchHost.host]))
    # GREATEST skips NULL: a host without a pause.
    slot = func.greatest(FetchHost.next_slot_at, FetchHost.paused_until, func.now())
    statement = (
        update(FetchHost)
        .where(FetchHost.host == host)
        .values(next_slot_at=slot + interval_seconds * _ONE_SECOND)
        .returning(FetchHost.next_slot_at, func.now())
    )
    next_slot_at, now = connection.execute(statement).one()
    return next_slot_at - timedelta(seconds=interval_seconds), now


def take_free_slot(connection: Connection, host: str, interval_seconds: float) -> datetime | None:
    """Send a request now if the host is free - no slot reserved ahead, no pause, the previous request at least
    an interval back - and record it; else when it is free next, for a backfill that leaves every slot to live
    requests. None when the request may go."""
    connection.execute(insert(FetchHost).values(host=host).on_conflict_do_nothing(index_elements=[FetchHost.host]))
    free_at = _free_at(interval_seconds)
    taken = (
        update(FetchHost)
        .where(FetchHost.host == host, free_at <= func.now())
        .values(next_slot_at=func.now() + interval_seconds * _ONE_SECOND, last_request_at=func.now())
        .returning(FetchHost.host)
    )
    if connection.execute(taken).first() is not None:
        return None
    return free_at_of(connection, host, interval_seconds)


def free_at_of(connection: Connection, host: str, interval_seconds: float) -> datetime:
    """When the host is free for a request that reserves no slot."""
    statement = select(func.greatest(_free_at(interval_seconds), func.now())).where(FetchHost.host == host)
    return connection.execute(statement).scalar_one_or_none() or connection.execute(select(func.now())).scalar_one()


def _free_at(interval_seconds: float):
    # GREATEST skips NULL: a host without a pause or without a request yet.
    last_request_ends = FetchHost.last_request_at + interval_seconds * _ONE_SECOND
    return func.greatest(FetchHost.next_slot_at, FetchHost.paused_until, last_request_ends)


def pause(connection: Connection, host: str, seconds: float) -> None:
    """No request to the host for `seconds` from now; a pause that lasts longer stays."""
    until = func.now() + seconds * _ONE_SECOND
    statement = insert(FetchHost).values(host=host, paused_until=until)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[FetchHost.host],
            set_={"paused_until": func.greatest(FetchHost.paused_until, statement.excluded.paused_until)},
        )
    )


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
