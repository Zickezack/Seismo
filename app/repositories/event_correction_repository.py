"""Data access for the corrections of the grouping (Korrekturen): merges and splits of events and the reports a
reprocessing replaced, the reports they move and the versions of the state they withdraw."""
from collections.abc import Iterable, Sequence

from sqlalchemy import Connection, Row, insert, or_, select, update

from app.db.models import Event, EventCandidate, EventCorrection, EventState


def insert_correction(connection: Connection, kind: str, event_id: int, moves: list[dict], reason: str | None) -> int:
    values = {"kind": kind, "event_id": event_id, "moves": moves, "reason": reason}
    return connection.execute(insert(EventCorrection).values(values).returning(EventCorrection.id)).scalar_one()


def of_event(connection: Connection, event_id: int) -> Sequence[Row]:
    """The corrections that moved reports to the event or from it, the earliest first."""
    statement = (
        select(EventCorrection.__table__)
        .where(or_(EventCorrection.event_id == event_id, EventCorrection.moves.contains([{"event_id": event_id}])))
        .order_by(EventCorrection.created_at, EventCorrection.id)
    )
    return connection.execute(statement).all()


def move_reports(connection: Connection, from_event_id: int, to_event_id: int, candidate_ids: Iterable[int] | None = None) -> list[int]:
    """Moves these reports of the event, or all of them, to the other; their ids. How each was assigned first stays
    with it."""
    statement = update(EventCandidate).where(EventCandidate.event_id == from_event_id)
    if candidate_ids is not None:
        statement = statement.where(EventCandidate.id.in_(list(candidate_ids)))
    moved = connection.execute(statement.values(event_id=to_event_id).returning(EventCandidate.id)).scalars()
    return sorted(moved)


def remove_reports(connection: Connection, event_id: int, candidate_ids: Iterable[int]) -> list[int]:
    """These reports leave the event for none - their reading was replaced; their ids. How each was assigned stays."""
    statement = (
        update(EventCandidate)
        .where(EventCandidate.event_id == event_id, EventCandidate.id.in_(list(candidate_ids)))
        .values(event_id=None)
        .returning(EventCandidate.id)
    )
    return sorted(connection.execute(statement).scalars())


def mark_merged(connection: Connection, event_ids: Iterable[int], into_id: int) -> None:
    connection.execute(update(Event).where(Event.id.in_(list(event_ids))).values(merged_into_id=into_id))


def withdraw_states(connection: Connection, state_ids: Iterable[int], correction_id: int) -> None:
    """The versions are no longer in effect; they stay, marked with the correction."""
    connection.execute(
        update(EventState)
        .where(EventState.id.in_(list(state_ids)), EventState.withdrawn_by_id.is_(None))
        .values(withdrawn_by_id=correction_id)
    )
