"""The start page (userstories.md, Dashboard, Recherche und Alerts, story 1): the events new and developing within a
period, so an analyst sees at a glance what is happening.

New: first reported within the period. Developing: reported before and again within it. The time of a report is its
article's publication, else its fetch, as everywhere. A developing event with a change of state within the period -
an escalation, a closure, a resumption: a material change - comes first; a version that only repeats the state, an
earlier report found later, is no change (event_service.mark_repeats). Then by how many sources reported within the
period - the breadth of the coverage -, then by how many reports, then the latest first.
"""
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, Row

from app.repositories import event_repository
from app.services.event_service import STATUS_LABELS, EventListItem

# The periods to choose from: the key in the URL, its label, its length.
PERIODS = {"24h": ("24 Stunden", timedelta(hours=24)), "3d": ("3 Tage", timedelta(days=3)), "7d": ("7 Tage", timedelta(days=7))}
DEFAULT_PERIOD = "24h"
# Events shown per list; the event list holds the rest.
LIST_LIMIT = 25


@dataclass(frozen=True)
class Change:
    """A change of an event's state."""

    status: str
    valid_from: datetime

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class DashboardEvent:
    event: EventListItem
    recent_reports: int  # within the period
    recent_sources: int
    changes: list[Change] = field(default_factory=list)  # within the period, the earliest first


@dataclass(frozen=True)
class Dashboard:
    period: str
    since: datetime
    new: list[DashboardEvent]  # the first LIST_LIMIT
    developing: list[DashboardEvent]
    new_total: int
    developing_total: int
    changed_total: int  # developing events with a change of state within the period

    @property
    def period_label(self) -> str:
        return PERIODS[self.period][0]


def parse_period(raw: str | None) -> str:
    """The period asked for; the default for anything else."""
    return raw if raw in PERIODS else DEFAULT_PERIOD


def overview(engine: Engine, period: str, now: datetime | None = None) -> Dashboard:
    period = parse_period(period)
    since = (now or datetime.now(UTC)) - PERIODS[period][1]
    with engine.connect() as connection:
        rows = event_repository.active_since(connection, since)
        versions = event_repository.versions_of(connection, [row.id for row in rows]) if rows else []
    changes = changes_since(versions, since)
    new, developing = [], []
    for row in rows:
        item = DashboardEvent(_list_item(row), row.recent_reports, row.recent_sources, changes.get(row.id, []))
        (new if row.first_reported_at >= since else developing).append(item)
    new.sort(key=lambda each: (-each.recent_sources, -each.recent_reports, -each.event.last_reported_at.timestamp(), each.event.id))
    developing.sort(key=lambda each: (
        not each.changes, -each.recent_sources, -each.recent_reports, -each.event.last_reported_at.timestamp(), each.event.id,
    ))
    return Dashboard(
        period, since, new[:LIST_LIMIT], developing[:LIST_LIMIT], len(new), len(developing),
        sum(bool(each.changes) for each in developing),
    )


def changes_since(versions, since: datetime) -> dict[int, list[Change]]:
    """Per event, its changes of state from `since` on: of its versions not withdrawn, each event's the earliest first,
    those whose status differs from the one before - the first founds the state and changes none."""
    changes: dict[int, list[Change]] = {}
    previous: dict[int, str] = {}
    for version in versions:
        before = previous.get(version.event_id)
        if before is not None and version.status != before and version.valid_from >= since:
            changes.setdefault(version.event_id, []).append(Change(version.status, version.valid_from))
        previous[version.event_id] = version.status
    return changes


def _list_item(row: Row) -> EventListItem:
    return EventListItem(**{each.name: row._mapping[each.name] for each in fields(EventListItem)})
