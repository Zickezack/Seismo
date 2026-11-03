"""Data access for the alert rules, their hits (Treffer) and the alerts they go out with."""
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import BigInteger, Connection, Row, cast, delete, exists, func, insert, literal, null, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Alert,
    AlertHit,
    AlertRule,
    AlertTrigger,
    Article,
    Event,
    EventCandidate,
    EventState,
    Extraction,
    Source,
    Watchlist,
)
from app.repositories import event_repository
from app.repositories.event_repository import EventFilter
from app.repositories.source_repository import NAME_COLLATION

# Of the two-key advisory lock that serialises the sweeps of the alert rules (7301 LLM slots, 7302 duplicate checks,
# 7303 assignments, 7304 entities).
ALERT_LOCK_NAMESPACE = 7305
UNIQUE_NAME_INDEX = "uq_alert_rule_name"

_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)
# The state or report a hit has none of, typed for INSERT ... SELECT.
_NO_ID = cast(null(), BigInteger)


class NameTaken(Exception):
    """Another alert rule already has this name, in any case."""


def try_lock_sweep(connection: Connection) -> bool:
    """Until the end of the transaction no other sweep runs; False when one runs already."""
    statement = text("SELECT pg_try_advisory_xact_lock(CAST(:namespace AS integer), 0)")
    return connection.execute(statement, {"namespace": ALERT_LOCK_NAMESPACE}).scalar_one()


def database_now(connection: Connection) -> datetime:
    return connection.execute(select(func.now())).scalar_one()


def insert_rule(connection: Connection, values: dict) -> int:
    with _name_must_be_free(connection):
        return connection.execute(insert(AlertRule).values(**values).returning(AlertRule.id)).scalar_one()


def update_rule(connection: Connection, rule_id: int, values: dict) -> bool:
    """Saving a rule starts it anew: what happened before no longer counts. False when it does not exist."""
    statement = update(AlertRule).where(AlertRule.id == rule_id).values(
        **values, active_since=func.now(), updated_at=func.now()
    )
    with _name_must_be_free(connection):
        return connection.execute(statement).rowcount == 1


def get_rule(connection: Connection, rule_id: int) -> Row | None:
    return connection.execute(select(AlertRule.__table__).where(AlertRule.id == rule_id)).one_or_none()


def rules(connection: Connection, active_only: bool = False) -> Sequence[Row]:
    """The rules by name, each with its watchlist's name, its hits waiting, its alerts and the latest one's time."""
    waiting = select(func.count()).where(AlertHit.rule_id == AlertRule.id, AlertHit.alert_id.is_(None)).scalar_subquery()
    alerts = select(func.count()).where(Alert.rule_id == AlertRule.id).scalar_subquery()
    latest = select(func.max(Alert.created_at)).where(Alert.rule_id == AlertRule.id).scalar_subquery()
    statement = (
        select(
            AlertRule.__table__, Watchlist.name.label("watchlist_name"), waiting.label("waiting_count"),
            alerts.label("alert_count"), latest.label("last_alert_at"),
        )
        .outerjoin(Watchlist, Watchlist.id == AlertRule.watchlist_id)
        .order_by(AlertRule.name.collate(NAME_COLLATION), AlertRule.id)
    )
    if active_only:
        statement = statement.where(AlertRule.is_active)
    return connection.execute(statement).all()


def lock_rules(connection: Connection, rule_ids: list[int]) -> Sequence[Row]:
    """Id, name and active flag of the existing ones, locked until the end of the transaction."""
    statement = (
        select(AlertRule.id, AlertRule.name, AlertRule.is_active)
        .where(AlertRule.id.in_(rule_ids))
        .order_by(AlertRule.id)
        .with_for_update()
    )
    return connection.execute(statement).all()


def delete_rules(connection: Connection, rule_ids: list[int]) -> int:
    """With their hits and alerts."""
    return connection.execute(delete(AlertRule).where(AlertRule.id.in_(rule_ids))).rowcount


def set_active(connection: Connection, rule_ids: list[int], active: bool) -> int:
    """An inactive rule finds nothing; activated again, it counts from then on, not what happened meanwhile."""
    values = {"is_active": active, "updated_at": func.now()}
    if active:
        values["active_since"] = func.now()
    statement = update(AlertRule).where(AlertRule.id.in_(rule_ids), AlertRule.is_active.is_not(active)).values(values)
    return connection.execute(statement).rowcount


def detect(connection: Connection, rule: Row, criteria: EventFilter, reported_since: datetime) -> int:
    """Store what the rule finds and has not found before - among the events reported from `reported_since` on that
    match it, of what happened since it is active - as hits waiting; returns how many. A hit is found once, so a
    sweep that runs again finds nothing twice."""
    matching = event_repository.matching_ids(criteria, reported_since)
    found = 0
    if rule.on_new:
        found += _store(connection, _new(rule, matching))
    if rule.on_change:
        found += _store(connection, _changes(rule, matching))
    if rule.on_report:
        found += _store(connection, _reports(rule, matching))
    return found


def _new(rule: Row, matching):
    """Events first reported since the rule is active; one made by a split is no new incident."""
    return select(literal(rule.id), Event.id, literal(AlertTrigger.NEW.value), _NO_ID, _NO_ID).where(
        Event.id.in_(matching),
        Event.first_reported_at >= rule.active_since,
        ~event_repository.split_off(Event.id),
        ~exists().where(AlertHit.rule_id == rule.id, AlertHit.event_id == Event.id, AlertHit.trigger == AlertTrigger.NEW),
    )


def _changes(rule: Row, matching):
    """Changes of state since the rule is active, as the dashboard counts them: a version not withdrawn whose status
    differs from the one before it - the first founds the state, a repeat is none. Only those a report's assignment
    made: a correction's replay restates what was known."""
    before = func.lag(EventState.status).over(partition_by=EventState.event_id, order_by=(EventState.valid_from, EventState.id))
    versions = (
        select(EventState.id, EventState.event_id, EventState.status, EventState.valid_from, EventState.added_by_id,
               before.label("before"))
        .where(EventState.event_id.in_(matching), EventState.withdrawn_by_id.is_(None))
        .subquery()
    )
    return select(
        literal(rule.id), versions.c.event_id, literal(AlertTrigger.CHANGE.value), versions.c.id, _NO_ID
    ).where(
        versions.c.before.is_not(None),
        versions.c.status != versions.c.before,
        versions.c.valid_from >= rule.active_since,
        versions.c.added_by_id.is_(None),
        ~exists().where(AlertHit.rule_id == rule.id, AlertHit.state_id == versions.c.id, AlertHit.trigger == AlertTrigger.CHANGE),
    )


def _reports(rule: Row, matching):
    """Reports made since the rule is active, of its article's first reading: a new reading of a reprocessing
    restates what was reported, and one it replaced is on its way out."""
    replaced = exists().where(Extraction.id == EventCandidate.extraction_id, Extraction.replaced_by_id.is_not(None))
    rereading = exists().where(Extraction.replaced_by_id == EventCandidate.extraction_id)
    return (
        select(literal(rule.id), EventCandidate.event_id, literal(AlertTrigger.REPORT.value), _NO_ID, EventCandidate.id)
        .join(Article, Article.id == EventCandidate.article_id)
        .where(
            EventCandidate.event_id.in_(matching),
            _MOMENT >= rule.active_since,
            ~replaced,
            ~rereading,
            ~exists().where(
                AlertHit.rule_id == rule.id, AlertHit.candidate_id == EventCandidate.id, AlertHit.trigger == AlertTrigger.REPORT
            ),
        )
    )


def _store(connection: Connection, found) -> int:
    columns = [AlertHit.rule_id, AlertHit.event_id, AlertHit.trigger, AlertHit.state_id, AlertHit.candidate_id]
    statement = pg_insert(AlertHit).from_select(columns, found).on_conflict_do_nothing().returning(AlertHit.id)
    return len(connection.execute(statement).all())


def waiting(connection: Connection, rule_id: int) -> Sequence[Row]:
    """The rule's hits not yet gone out, the earliest first."""
    statement = (
        select(AlertHit.id, AlertHit.event_id, AlertHit.trigger, AlertHit.detected_at)
        .where(AlertHit.rule_id == rule_id, AlertHit.alert_id.is_(None))
        .order_by(AlertHit.detected_at, AlertHit.id)
    )
    return connection.execute(statement).all()


def sent_since(connection: Connection, rule_id: int, since: datetime) -> int:
    """The rule's alerts from `since` on."""
    statement = select(func.count()).where(Alert.rule_id == rule_id, Alert.created_at >= since)
    return connection.execute(statement).scalar_one()


def last_alerted(connection: Connection, rule_id: int, event_ids: Iterable[int], since: datetime) -> dict[int, datetime]:
    """Per event, when the rule's latest alert from `since` on with a hit of it went out."""
    statement = (
        select(AlertHit.event_id, func.max(Alert.created_at))
        .join(Alert, Alert.id == AlertHit.alert_id)
        .where(AlertHit.rule_id == rule_id, AlertHit.event_id.in_(list(event_ids)), Alert.created_at >= since)
        .group_by(AlertHit.event_id)
    )
    return dict(connection.execute(statement).all())


def send(connection: Connection, rule_id: int, hit_ids: list[int], digest: bool) -> int:
    """One alert with these hits."""
    alert_id = connection.execute(insert(Alert).values(rule_id=rule_id, digest=digest).returning(Alert.id)).scalar_one()
    connection.execute(update(AlertHit).where(AlertHit.id.in_(hit_ids)).values(alert_id=alert_id))
    return alert_id


def unread_count(connection: Connection) -> int:
    return connection.execute(select(func.count()).where(Alert.read_at.is_(None))).scalar_one()


def alerts(connection: Connection, unread_only: bool, limit: int) -> Sequence[Row]:
    """The latest alerts, the newest first, each with its rule's name."""
    statement = (
        select(Alert.__table__, AlertRule.name.label("rule_name"))
        .join(AlertRule, AlertRule.id == Alert.rule_id)
        .order_by(Alert.created_at.desc(), Alert.id.desc())
        .limit(limit)
    )
    if unread_only:
        statement = statement.where(Alert.read_at.is_(None))
    return connection.execute(statement).all()


def count_alerts(connection: Connection, unread_only: bool) -> int:
    statement = select(func.count()).select_from(Alert)
    if unread_only:
        statement = statement.where(Alert.read_at.is_(None))
    return connection.execute(statement).scalar_one()


def hits_of(connection: Connection, alert_ids: Iterable[int]) -> Sequence[Row]:
    """The hits of these alerts with their event's title, the status a change went to, and a report's title, article
    and source; each alert's in the order they were found."""
    statement = (
        select(
            AlertHit.alert_id, AlertHit.event_id, AlertHit.trigger, AlertHit.detected_at,
            Event.title.label("event_title"), Event.merged_into_id,
            EventState.status.label("state_status"), EventState.valid_from.label("state_from"),
            EventCandidate.title.label("report_title"), Article.id.label("article_id"),
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
        )
        .join(Event, Event.id == AlertHit.event_id)
        .outerjoin(EventState, EventState.id == AlertHit.state_id)
        .outerjoin(EventCandidate, EventCandidate.id == AlertHit.candidate_id)
        .outerjoin(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(AlertHit.alert_id.in_(list(alert_ids)))
        .order_by(AlertHit.alert_id, AlertHit.detected_at, AlertHit.id)
    )
    return connection.execute(statement).all()


def mark_read(connection: Connection, alert_ids: list[int] | None = None) -> int:
    """These alerts, or every unread one, read now; returns how many were unread."""
    statement = update(Alert).where(Alert.read_at.is_(None)).values(read_at=func.now())
    if alert_ids is not None:
        statement = statement.where(Alert.id.in_(alert_ids))
    return connection.execute(statement).rowcount


def existing_alerts(connection: Connection, alert_ids: list[int]) -> list[int]:
    statement = select(Alert.id).where(Alert.id.in_(alert_ids)).with_for_update()
    return list(connection.execute(statement).scalars())


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
