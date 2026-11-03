"""Alerts (userstories.md, Dashboard, Recherche und Alerts, story 3; requirements.md, Dashboard, Recherche und Alerts,
criterion 3): an alert rule covers the events of its watchlist, if it has one, that match its filter - the filter of
the event list, without period - and notifies in the app about what happens to them while it is active:

- Neues Ereignis: an event matches it for the first time, first reported since the rule is active - also when it
  reaches the relevance asked for only later, its third source say.
- Zustandswechsel: a change of the event's state - an escalation, a closure, a resumption - as the start page counts
  them: a version whose status differs from the one before; a repeat is none. Only those a report's assignment
  appended: a correction's replay only restates what was known.
- Neuer Bericht: a report of the event, of its article's first reading - a reprocessing's new reading restates it.

What it finds is a hit (Treffer), each found once. Throttled (the admin's decision of 2026-11-03): an event's hits go
out together as one alert, an event again only after the rule's quiet period - a change of state at once -, and at
most the rule's number of alerts per hour; beyond it, the hits due go out together as one Sammel-Alert as soon as the
hour allows. Nothing is dropped: a hit waits until it can go out. The worker looks once a minute (`sweep`).

What happened before the rule was active does not count: saving or activating it again starts it anew. A backfill's
old reports, a split's new event and a correction's versions therefore never alert.
"""
import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import Engine, Row

from app.db.models import AlertTrigger
from app.repositories import alert_repository, watchlist_repository
from app.repositories.alert_repository import NameTaken
from app.repositories.event_repository import EventFilter
from app.services import event_service
from app.services.bulk import BulkResult
from app.services.extraction_service import STATUS_LABELS

logger = logging.getLogger(__name__)

# The events a sweep looks at: reported within this. Longer than an event stays open (event_service.OPEN_FOR), so an
# entity recognised late, or the source that makes an event relevant, still counts.
LOOKBACK = timedelta(days=14)
HOUR = timedelta(hours=1)
LIST_LIMIT = 100
MAX_NAME_CHARS = 200
MAX_PER_HOUR = (1, 100)
COOLDOWN_HOURS = (0, 168)
DEFAULTS = {"max_per_hour": 5, "cooldown_hours": 6, "on_new": True, "on_change": True, "on_report": False}
TRIGGER_LABELS = {AlertTrigger.NEW: "Neues Ereignis", AlertTrigger.CHANGE: "Zustandswechsel", AlertTrigger.REPORT: "Neuer Bericht"}
# The filter's fields a rule keeps: the event list's but for period and development (and the watchlist, a field of
# its own).
_FILTER_FIELDS = ("situation", "type", "country", "sector", "company", "sources", "impact", "horizon")


class InvalidRule(ValueError):
    """Invalid entries from the alert rule form. The message is shown to the analyst as is."""


@dataclass(frozen=True)
class RuleValues:
    name: str
    is_active: bool
    watchlist_id: int | None
    criteria: dict[str, str]
    on_new: bool
    on_change: bool
    on_report: bool
    max_per_hour: int
    cooldown_hours: int

    def row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RuleView:
    id: int
    name: str
    is_active: bool
    watchlist_id: int | None
    watchlist_name: str | None
    criteria: dict[str, str]
    on_new: bool
    on_change: bool
    on_report: bool
    max_per_hour: int
    cooldown_hours: int
    active_since: datetime
    waiting_count: int = 0
    alert_count: int = 0
    last_alert_at: datetime | None = None

    @property
    def trigger_labels(self) -> list[str]:
        chosen = {AlertTrigger.NEW: self.on_new, AlertTrigger.CHANGE: self.on_change, AlertTrigger.REPORT: self.on_report}
        return [TRIGGER_LABELS[trigger] for trigger, on in chosen.items() if on]

    @property
    def filter_query(self) -> str:
        """The rule's events on the event list: its filter with its watchlist."""
        form = event_service.parse_filter(self.criteria | ({"watchlist": str(self.watchlist_id)} if self.watchlist_id else {}))
        return form.query()

    @property
    def throttle_label(self) -> str:
        quiet = f"ein Ereignis erneut nach {self.cooldown_hours} h" if self.cooldown_hours else "ohne Ruhezeit"
        return f"höchstens {self.max_per_hour} pro Stunde, {quiet}"


@dataclass(frozen=True)
class Waiting:
    """A hit not yet gone out."""

    id: int
    event_id: int
    trigger: str
    detected_at: datetime


@dataclass(frozen=True)
class Delivery:
    """An alert to send: these hits; a Sammel-Alert when the rule's hourly limit kept them from going out one event
    each."""

    hit_ids: list[int]
    digest: bool = False


def throttle(
    waiting: Iterable[Waiting], last_alerted: Mapping[int, datetime], sent_last_hour: int, max_per_hour: int,
    cooldown: timedelta, now: datetime,
) -> list[Delivery]:
    """What goes out now. An event's hits go together. An event alerted within the quiet period waits for its end -
    unless it changed its state. Of those due, the changes first, then the earliest found; one alert each while the
    hourly limit allows it, the last one allowed taking all the rest as a Sammel-Alert; at the limit, all wait."""
    groups: dict[int, list[Waiting]] = {}
    for hit in sorted(waiting, key=lambda each: (each.detected_at, each.id)):
        groups.setdefault(hit.event_id, []).append(hit)

    def due(event_id: int, hits: list[Waiting]) -> bool:
        last = last_alerted.get(event_id)
        return last is None or now - last >= cooldown or any(hit.trigger == AlertTrigger.CHANGE for hit in hits)

    ready = [hits for event_id, hits in groups.items() if due(event_id, hits)]
    ready.sort(key=lambda hits: (all(hit.trigger != AlertTrigger.CHANGE for hit in hits), hits[0].detected_at, hits[0].id))
    allowed = max_per_hour - sent_last_hour
    if allowed <= 0 or not ready:
        return []
    if len(ready) <= allowed:
        return [Delivery([hit.id for hit in hits]) for hits in ready]
    single, rest = ready[:allowed - 1], ready[allowed - 1:]
    return [Delivery([hit.id for hit in hits]) for hits in single] + [Delivery([hit.id for hits in rest for hit in hits], True)]


def sweep(engine: Engine) -> int:
    """Find what every active rule finds and send what its throttle lets go; returns the number of alerts sent. One
    sweep at a time across all workers: two would send the same hits twice."""
    sent = 0
    with engine.begin() as connection:
        if not alert_repository.try_lock_sweep(connection):
            return 0
        now = alert_repository.database_now(connection)
        for rule in alert_repository.rules(connection, active_only=True):
            alert_repository.detect(connection, rule, criteria_of(rule), now - LOOKBACK)
            waiting = [Waiting(row.id, row.event_id, row.trigger, row.detected_at) for row in alert_repository.waiting(connection, rule.id)]
            if not waiting:
                continue
            cooldown = timedelta(hours=rule.cooldown_hours)
            last = alert_repository.last_alerted(connection, rule.id, {hit.event_id for hit in waiting}, now - cooldown)
            sent_last_hour = alert_repository.sent_since(connection, rule.id, now - HOUR)
            for delivery in throttle(waiting, last, sent_last_hour, rule.max_per_hour, cooldown, now):
                alert_repository.send(connection, rule.id, delivery.hit_ids, delivery.digest)
                sent += 1
    if sent:
        logger.info("Alerts sent", extra={"alerts": sent})
    return sent


def criteria_of(rule: Row | RuleView) -> EventFilter:
    """What the rule's events match: its filter, without period, and its watchlist."""
    criteria = event_service.parse_filter(rule.criteria, with_period=False).criteria
    return replace(criteria, watchlist_id=rule.watchlist_id)


def parse(form: Mapping[str, str]) -> RuleValues:
    """The rule form's entries, checked; InvalidRule names the first wrong field."""
    name = " ".join((form.get("name") or "").split())
    if not name:
        raise InvalidRule("Name: bitte angeben.")
    if len(name) > MAX_NAME_CHARS:
        raise InvalidRule(f"Name: höchstens {MAX_NAME_CHARS} Zeichen.")
    raw_watchlist = (form.get("watchlist") or "").strip()
    if raw_watchlist and not raw_watchlist.isdigit():
        raise InvalidRule("Watchlist: bitte eine aus der Liste wählen.")
    triggers = {key: bool(form.get(key)) for key in ("on_new", "on_change", "on_report")}
    if not any(triggers.values()):
        raise InvalidRule("Melden bei: bitte mindestens eines wählen.")
    filter_values = event_service.parse_filter({key: form[key] for key in _FILTER_FIELDS if form.get(key)}, with_period=False).values
    return RuleValues(
        name=name,
        is_active=bool(form.get("is_active")),
        watchlist_id=int(raw_watchlist) if raw_watchlist else None,
        criteria={key: value for key, value in filter_values.items() if key in _FILTER_FIELDS},
        max_per_hour=_number(form.get("max_per_hour"), "Höchstens pro Stunde", *MAX_PER_HOUR),
        cooldown_hours=_number(form.get("cooldown_hours"), "Ruhezeit pro Ereignis (Stunden)", *COOLDOWN_HOURS),
        **triggers,
    )


def _number(raw: str | None, label: str, low: int, high: int) -> int:
    text = (raw or "").strip()
    if not text.isdigit() or not low <= int(text) <= high:
        raise InvalidRule(f"{label}: eine ganze Zahl von {low} bis {high}.")
    return int(text)


def create(engine: Engine, form: Mapping[str, str]) -> RuleView:
    values = parse(form)
    try:
        with engine.begin() as connection:
            _check_watchlist(connection, values.watchlist_id)
            rule_id = alert_repository.insert_rule(connection, values.row())
            return _view(alert_repository.get_rule(connection, rule_id))
    except NameTaken:
        raise InvalidRule(_name_taken(values.name)) from None


def update(engine: Engine, rule_id: int, form: Mapping[str, str]) -> RuleView | None:
    """Save every field; the rule starts anew. None when it no longer exists."""
    values = parse(form)
    try:
        with engine.begin() as connection:
            _check_watchlist(connection, values.watchlist_id)
            if not alert_repository.update_rule(connection, rule_id, values.row()):
                return None
            return _view(alert_repository.get_rule(connection, rule_id))
    except NameTaken:
        raise InvalidRule(_name_taken(values.name)) from None


def _check_watchlist(connection, watchlist_id: int | None) -> None:
    if watchlist_id is not None and watchlist_repository.get(connection, watchlist_id) is None:
        raise InvalidRule("Watchlist: nicht gefunden.")


def get(engine: Engine, rule_id: int) -> RuleView | None:
    with engine.connect() as connection:
        row = alert_repository.get_rule(connection, rule_id)
    return _view(row) if row else None


def rules(engine: Engine) -> list[RuleView]:
    with engine.connect() as connection:
        return [_view(row) for row in alert_repository.rules(connection)]


def new_rule(params: Mapping[str, str]) -> dict:
    """What the form of a new rule starts with: the defaults, and the filter it was asked from (the event list's
    "Als Alert-Regel speichern")."""
    values = event_service.parse_filter(params, with_period=False).values
    return DEFAULTS | {"is_active": True, "criteria": {key: values[key] for key in _FILTER_FIELDS if key in values},
                       "watchlist_id": int(values["watchlist"]) if "watchlist" in values else None}


def set_active(engine: Engine, rule_ids: Iterable[int], active: bool) -> BulkResult:
    return _bulk(engine, rule_ids, lambda connection, ids: alert_repository.set_active(connection, ids, active))


def delete(engine: Engine, rule_ids: Iterable[int]) -> BulkResult:
    """With their hits and alerts."""
    return _bulk(engine, rule_ids, alert_repository.delete_rules)


def _bulk(engine: Engine, rule_ids: Iterable[int], action) -> BulkResult:
    """All or nothing (requirements.md, "Destructive and Bulk Actions"): one not found blocks all."""
    rule_ids = sorted(set(rule_ids))
    if not rule_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id for row in alert_repository.lock_rules(connection, rule_ids)}
        blocked = [f"Alert-Regel #{each} (nicht gefunden)" for each in rule_ids if each not in found]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        action(connection, rule_ids)
    return BulkResult(processed=len(rule_ids), blocked=[])


def _name_taken(name: str) -> str:
    return f"Name: „{name}“ ist bereits vergeben."


def _view(row: Row) -> RuleView:
    mapping = row._mapping
    return RuleView(
        id=row.id, name=row.name, is_active=row.is_active, watchlist_id=row.watchlist_id,
        watchlist_name=mapping.get("watchlist_name"), criteria=dict(row.criteria), on_new=row.on_new,
        on_change=row.on_change, on_report=row.on_report, max_per_hour=row.max_per_hour,
        cooldown_hours=row.cooldown_hours, active_since=row.active_since, waiting_count=mapping.get("waiting_count", 0),
        alert_count=mapping.get("alert_count", 0), last_alert_at=mapping.get("last_alert_at"),
    )


# The inbox.

@dataclass(frozen=True)
class ReportHit:
    title: str
    article_id: int
    source: str | None


@dataclass
class AlertEvent:
    """What an alert says of one event."""

    event_id: int
    title: str
    merged_into_id: int | None
    new: bool = False
    changes: list[str] = field(default_factory=list)  # the statuses it changed to, the earliest first
    reports: list[ReportHit] = field(default_factory=list)


@dataclass(frozen=True)
class AlertView:
    id: int
    rule_id: int
    rule_name: str
    digest: bool
    created_at: datetime
    read_at: datetime | None
    events: list[AlertEvent]

    @property
    def summary(self) -> str:
        """What it is about, in a few words: "Neues Ereignis", "3 Ereignisse", ..."""
        if len(self.events) != 1:
            return f"{len(self.events)} Ereignisse"
        event = self.events[0]
        parts = (["Neues Ereignis"] if event.new else []) + [f"Zustand: {status}" for status in event.changes]
        if event.reports:
            parts.append("1 neuer Bericht" if len(event.reports) == 1 else f"{len(event.reports)} neue Berichte")
        return ", ".join(parts)


@dataclass(frozen=True)
class Inbox:
    alerts: list[AlertView]  # the latest LIST_LIMIT
    total: int
    unread: int
    unread_only: bool


def unread_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return alert_repository.unread_count(connection)


def inbox(engine: Engine, unread_only: bool) -> Inbox:
    with engine.connect() as connection:
        rows = alert_repository.alerts(connection, unread_only, LIST_LIMIT)
        hits = alert_repository.hits_of(connection, [row.id for row in rows]) if rows else []
        total = alert_repository.count_alerts(connection, unread_only)
        unread = alert_repository.unread_count(connection)
    events: dict[int, dict[int, AlertEvent]] = {}
    for hit in hits:
        event = events.setdefault(hit.alert_id, {}).setdefault(
            hit.event_id, AlertEvent(hit.event_id, hit.event_title, hit.merged_into_id)
        )
        if hit.trigger == AlertTrigger.NEW:
            event.new = True
        elif hit.trigger == AlertTrigger.CHANGE:
            event.changes.append(STATUS_LABELS[hit.state_status])
        else:
            event.reports.append(ReportHit(hit.report_title, hit.article_id, hit.source_display_name))
    alerts = [
        AlertView(row.id, row.rule_id, row.rule_name, row.digest, row.created_at, row.read_at, list(events.get(row.id, {}).values()))
        for row in rows
    ]
    return Inbox(alerts, total, unread, unread_only)


def mark_read(engine: Engine, alert_ids: Iterable[int]) -> BulkResult:
    """All or nothing: one not found blocks all; one read already stays as it was."""
    alert_ids = sorted(set(alert_ids))
    if not alert_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = set(alert_repository.existing_alerts(connection, alert_ids))
        blocked = [f"Alert #{each} (nicht gefunden)" for each in alert_ids if each not in found]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        alert_repository.mark_read(connection, alert_ids)
    return BulkResult(processed=len(alert_ids), blocked=[])


def mark_all_read(engine: Engine) -> int:
    with engine.begin() as connection:
        return alert_repository.mark_read(connection)
