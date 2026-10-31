"""Corrections of the grouping (userstories.md, Ereignisidentität und zeitliche Entwicklung, story 3): the analyst merges
events that are one incident and splits off reports that are another. Every correction is recorded (Korrektur,
datamodel.md, rule 3) with the reports it moved and the events they left.

A merge keeps the event it is made on: the others' reports go to it, and each of them stays as an event merged into
it, so its id still leads there. A split makes the selected reports one new event, named after its earliest report;
the report an event is named after never leaves it, so its id stays with the incident it was given for.

The state history of every event a correction concerns becomes what its reports make when weighed one by one in the
order they were assigned (replay), by the rule of the assignment (event_service.state_effect) - as if they had been
grouped so from the start. A version they no longer make is withdrawn: kept, marked with the correction, never in
effect again. One they make anew is appended. Nothing is changed or deleted (the admin's decision of 2026-10-30).

A correction runs under the assignment's lock, so a report is never assigned to an event while it is merged away: an
assignment decided before compares again under the lock (event_service.assign).

A reprocessing (reprocessing_service, userstories.md, Verdichtung und Daten-Lifecycle, story 2) corrects by the same
rule: once an article's new reading has its events, the reports of the one it replaced leave theirs (replace_reports),
recorded as a Korrektur of its own.
"""
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, Row

from app.db.models import CorrectionKind
from app.repositories import event_correction_repository, event_repository
from app.services import event_service
from app.services.bulk import BulkResult
from app.services.event_service import OPEN_FOR, EventListItem, StateEffect, state_effect

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 1000
# Shown on the event page: enough to find the event an assignment missed, few enough to read.
SIMILAR_LIMIT = 10


class InvalidReason(ValueError):
    """A reason too long: nothing is corrected."""


@dataclass(frozen=True)
class SplitResult(BulkResult):
    event_id: int | None = None  # the new event


@dataclass(frozen=True)
class Version:
    status: str
    valid_from: datetime
    candidate_id: int


@dataclass(frozen=True)
class SimilarEvent:
    event: EventListItem
    score: float

    @property
    def score_label(self) -> str:
        return event_service.score_label(self.score)


def similar_events(engine: Engine, event_id: int) -> list[SimilarEvent]:
    """The events reported from OPEN_FOR before this one's first report to OPEN_FOR after its last - those a report of
    it was compared with - by their similarity to it, the most similar first: what the analyst merges from. None for a
    merged event."""
    with engine.connect() as connection:
        event = event_repository.get(connection, event_id)
        if event is None or event.merged_into_id is not None:
            return []
        reports = event_repository.reports_between(connection, event.first_reported_at - OPEN_FOR, event.last_reported_at + OPEN_FOR)
        own = [report for report in reports if report.event_id == event_id]
        found = event_service.related(own, [report for report in reports if report.event_id != event_id])[:SIMILAR_LIMIT]
    events = {item.id: item for item in event_service.listed(engine, [match.event_id for match in found])}
    # One merged meanwhile is no longer listed.
    return [SimilarEvent(events[match.event_id], match.score) for match in found if match.event_id in events]


def merge(engine: Engine, event_id: int, other_ids: Iterable[int], reason: str = "") -> BulkResult:
    """Zusammenführen: the reports of the other events go to this one, all or none. Each of them stays as an event
    merged into this one. InvalidReason for a reason too long."""
    reason = _reason(reason)
    other_ids = sorted(set(other_ids) - {event_id})
    if not other_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        event_repository.lock_assignment(connection)
        events = event_repository.for_correction(connection, [event_id, *other_ids])
        blocked = [blocker for each in [event_id, *other_ids] if (blocker := _blocker(events.get(each), each))]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        moves = [
            {"event_id": other, "candidate_ids": event_correction_repository.move_reports(connection, other, event_id)}
            for other in other_ids
        ]
        event_correction_repository.mark_merged(connection, other_ids, event_id)
        correction_id = event_correction_repository.insert_correction(connection, CorrectionKind.MERGE, event_id, moves, reason)
        event_repository.refresh_reach(connection, event_id)
        _reconcile(connection, event_id, correction_id)
    # IDs only (datamodel.md, rule 13): never a title or the reason.
    reports = sum(len(move["candidate_ids"]) for move in moves)
    logger.info("Events merged", extra={"event_id": event_id, "merged": other_ids, "reports": reports, "correction_id": correction_id})
    return BulkResult(processed=len(other_ids), blocked=[])


def split(engine: Engine, event_id: int, candidate_ids: Iterable[int], reason: str = "") -> SplitResult:
    """Abspalten: the selected reports of the event become one new event, named after the earliest of them - all or
    none. The report the event is named after stays. InvalidReason for a reason too long."""
    reason = _reason(reason)
    candidate_ids = sorted(set(candidate_ids))
    if not candidate_ids:
        return SplitResult(processed=0, blocked=[])
    with engine.begin() as connection:
        event_repository.lock_assignment(connection)
        event = event_repository.for_correction(connection, [event_id]).get(event_id)
        if blocker := _blocker(event, event_id):
            return SplitResult(processed=0, blocked=[blocker])
        reports = {report.id: report for report in event_repository.assigned_reports(connection, event_id)}
        # A report not here any more: moved by another correction meanwhile.
        blocked = [f"Bericht {candidate_id} (nicht in diesem Ereignis)" for candidate_id in candidate_ids if candidate_id not in reports]
        if event.founding_candidate_id in candidate_ids and event.founding_candidate_id in reports:
            blocked.append(f"{reports[event.founding_candidate_id].title} (benennt das Ereignis)")
        if not blocked and len(candidate_ids) == len(reports):
            blocked.append(f"{event.title} (behielte keinen Bericht)")
        if blocked:
            return SplitResult(processed=0, blocked=blocked)
        moved = [reports[candidate_id] for candidate_id in candidate_ids]
        founding = min(moved, key=lambda report: (report.moment, report.id))
        new_id = event_repository.create_event(connection, {
            "title": founding.title, "event_type": founding.event_type, "country": founding.country,
            "first_reported_at": founding.moment, "last_reported_at": max(report.moment for report in moved),
            "founding_candidate_id": founding.id,
        })
        event_correction_repository.move_reports(connection, event_id, new_id, candidate_ids)
        moves = [{"event_id": event_id, "candidate_ids": candidate_ids}]
        correction_id = event_correction_repository.insert_correction(connection, CorrectionKind.SPLIT, new_id, moves, reason)
        event_repository.refresh_reach(connection, event_id)
        _reconcile(connection, event_id, correction_id)
        _reconcile(connection, new_id, correction_id)
    logger.info("Event split", extra={"event_id": event_id, "new_event_id": new_id, "reports": len(candidate_ids), "correction_id": correction_id})
    return SplitResult(processed=len(candidate_ids), blocked=[], event_id=new_id)


def replace_reports(
    connection: Connection, event_id: int, candidate_ids: list[int], article_id: int, successor_event_id: int | None
) -> None:
    """Neuverarbeitung: the reports leave the event, their article's new reading having its events by now - in the
    caller's transaction, under the assignment's lock. The event gets the history its remaining reports make. Named
    after one of them, it is named after the new reading's report in it, else after its earliest; one left without
    reports is marked merged into `successor_event_id`, where the new reading's first report went."""
    event = event_repository.for_correction(connection, [event_id]).get(event_id)
    if event is None:
        return
    removed = event_correction_repository.remove_reports(connection, event_id, candidate_ids)
    if not removed:
        return
    correction_id = event_correction_repository.insert_correction(
        connection, CorrectionKind.REPROCESS, event_id, [{"event_id": event_id, "candidate_ids": removed}], None
    )
    remaining = list(event_repository.assigned_reports(connection, event_id))
    if not remaining:
        if successor_event_id is not None and successor_event_id != event_id:
            event_correction_repository.mark_merged(connection, [event_id], successor_event_id)
    elif event.founding_candidate_id is None or event.founding_candidate_id in removed:
        successors = [report for report in remaining if report.article_id == article_id]
        founding = successors[0] if successors else min(remaining, key=lambda report: (report.moment, report.id))
        event_repository.name_after(connection, event_id, founding)
    event_repository.refresh_reach(connection, event_id)
    _reconcile(connection, event_id, correction_id)


def replay(reports: Iterable) -> list[Version]:
    """The versions the reports make, given in the order they were assigned: what assigning them one by one to one
    event makes (event_service._apply), each weighed against the version in effect at its own time. The earliest
    first; of equal times the one made last is in effect, as in the database (event_repository._IN_EFFECT_ORDER)."""
    timeline: list[Version] = []
    for report in reports:
        in_effect = [version.status for version in timeline if version.valid_from <= report.moment]
        if state_effect(in_effect[-1] if in_effect else None, report.status) == StateEffect.VERSION:
            timeline.append(Version(report.status, report.moment, report.id))
            timeline.sort(key=lambda version: version.valid_from)  # stable: equal times stay in the order made
    return timeline


def _reconcile(connection: Connection, event_id: int, correction_id: int) -> None:
    """The event's history as its reports make it now: the versions they no longer make are withdrawn by the
    correction, the ones they make anew appended by it, recorded now."""
    made = replay(event_repository.assigned_reports(connection, event_id))
    current = {(state.status, state.valid_from, state.candidate_id): state.id for state in event_repository.states_in_effect(connection, event_id)}
    kept = {(version.status, version.valid_from, version.candidate_id) for version in made}
    event_correction_repository.withdraw_states(connection, [state_id for key, state_id in current.items() if key not in kept], correction_id)
    for version in made:
        if (version.status, version.valid_from, version.candidate_id) not in current:
            event_repository.add_state(connection, event_id, version.status, version.valid_from, version.candidate_id, correction_id)


def _blocker(event: Row | None, event_id: int) -> str | None:
    """What keeps an event from a correction: gone, or merged into another already."""
    if event is None:
        return f"Ereignis {event_id} (nicht gefunden)"
    if event.merged_into_id is not None:
        return f"{event.title} (bereits zusammengeführt)"
    return None


def _reason(text: str | None) -> str | None:
    reason = (text or "").strip()
    if len(reason) > MAX_REASON_CHARS:
        raise InvalidReason(f"Die Begründung hat {len(reason)} Zeichen, höchstens {MAX_REASON_CHARS} sind möglich.")
    return reason or None
