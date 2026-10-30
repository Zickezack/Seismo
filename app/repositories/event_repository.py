"""Data access for the events (Ereignisse) and the assignment of event candidates to them."""
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import Connection, Row, exists, func, insert, literal, select, text, update
from sqlalchemy.orm import aliased

from app.db.models import Article, Event, EventCandidate, EventState, Job, Source

# Of the two-key advisory lock that serialises the assignments (7301 LLM slots, 7302 duplicate checks).
EVENT_LOCK_NAMESPACE = 7303

# When a report was made: its article's publication, else its fetch.
_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)
# The version of an event's state in effect at a moment: the latest from before it, of equal times the one recorded last.
# A version a correction withdrew is never in effect.
_IN_EFFECT_ORDER = (EventState.valid_from.desc(), EventState.id.desc())
_NOT_WITHDRAWN = EventState.withdrawn_by_id.is_(None)
# An event's state now: its latest version.
_CURRENT_STATE = (
    select(EventState.status).where(EventState.event_id == Event.id, _NOT_WITHDRAWN).order_by(*_IN_EFFECT_ORDER).limit(1)
    .scalar_subquery().label("state")
)


def lock_assignment(connection: Connection) -> None:
    """Until the end of the transaction no other one assigns, so two never found an event for the same incident."""
    statement = text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), 0)")
    connection.execute(statement, {"namespace": EVENT_LOCK_NAMESPACE})


def due(connection: Connection, job_kind: str, limit: int) -> list[int]:
    """Candidates without an event and never queued, the earliest report first."""
    statement = (
        select(EventCandidate.id)
        .join(Article, Article.id == EventCandidate.article_id)
        .where(
            EventCandidate.event_id.is_(None),
            # Any job of it, failed ones too: a failed one comes back only when the admin requeues it.
            ~exists().where(Job.dedupe_key == func.concat(literal(f"{job_kind}:"), EventCandidate.id)),
        )
        .order_by(_MOMENT, EventCandidate.id)
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def count_unassigned(connection: Connection) -> int:
    statement = select(func.count()).select_from(EventCandidate).where(EventCandidate.event_id.is_(None))
    return connection.execute(statement).scalar_one()


def get_for_assignment(connection: Connection, candidate_id: int) -> Row | None:
    """What a candidate is compared by, its event if it has one, when it was reported, and the state it reports."""
    statement = (
        select(
            EventCandidate.id,
            EventCandidate.article_id,
            EventCandidate.event_id,
            EventCandidate.event_type,
            EventCandidate.status,
            EventCandidate.title,
            EventCandidate.summary,
            EventCandidate.country,
            EventCandidate.place,
            EventCandidate.time_note,
            EventCandidate.actors,
            _MOMENT.label("moment"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .where(EventCandidate.id == candidate_id)
    )
    return connection.execute(statement).one_or_none()


def reports_near(connection: Connection, moment: datetime, window: timedelta) -> Sequence[Row]:
    """The reports of the events reported from `window` before `moment` to `window` after it: what a report of that
    moment is compared with."""
    return reports_between(connection, moment - window, moment + window)


def reports_between(connection: Connection, start: datetime, end: datetime) -> Sequence[Row]:
    """The reports of the events whose reports reach into the time from `start` to `end`, with what they are compared
    by."""
    statement = (
        select(
            EventCandidate.event_id,
            EventCandidate.article_id,
            EventCandidate.title,
            EventCandidate.summary,
            EventCandidate.place,
            EventCandidate.actors,
        )
        .join(Event, Event.id == EventCandidate.event_id)
        .where(Event.last_reported_at >= start, Event.first_reported_at <= end)
    )
    return connection.execute(statement).all()


def events_for_choice(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """What the LLM is shown of the events to choose from: name, first and last report, the latest report's summary."""
    event_ids = list(event_ids)
    if not event_ids:
        return []
    latest_report = aliased(EventCandidate)
    latest_article = aliased(Article)
    latest_summary = (
        select(latest_report.summary)
        .join(latest_article, latest_article.id == latest_report.article_id)
        .where(latest_report.event_id == Event.id)
        .order_by(func.coalesce(latest_article.published_at, latest_article.fetched_at).desc(), latest_report.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    statement = select(
        Event.id, Event.title, Event.first_reported_at, Event.last_reported_at, latest_summary.label("latest_summary")
    ).where(Event.id.in_(event_ids))
    return connection.execute(statement).all()


def create_event(connection: Connection, values: dict) -> int:
    return connection.execute(insert(Event).values(values).returning(Event.id)).scalar_one()


def assign(connection: Connection, candidate_id: int, event_id: int, method: str, score: float | None, moment: datetime) -> None:
    """The candidate joins the event, whose reports reach as far as its own from now on."""
    connection.execute(
        update(EventCandidate)
        .where(EventCandidate.id == candidate_id, EventCandidate.event_id.is_(None))
        .values(event_id=event_id, assignment_method=method, assignment_score=score, assigned_at=func.now())
    )
    connection.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(
            first_reported_at=func.least(Event.first_reported_at, moment),
            last_reported_at=func.greatest(Event.last_reported_at, moment),
        )
    )


def state_at(connection: Connection, event_id: int, moment: datetime) -> str | None:
    """The status of the version in effect at `moment`; None before the event's first version."""
    statement = (
        select(EventState.status)
        .where(EventState.event_id == event_id, EventState.valid_from <= moment, _NOT_WITHDRAWN)
        .order_by(*_IN_EFFECT_ORDER)
        .limit(1)
    )
    return connection.execute(statement).scalar_one_or_none()


def add_state(
    connection: Connection, event_id: int, status: str, valid_from: datetime, candidate_id: int, added_by_id: int | None = None
) -> None:
    values = {"event_id": event_id, "status": status, "valid_from": valid_from, "candidate_id": candidate_id, "added_by_id": added_by_id}
    connection.execute(insert(EventState).values(values))


def states(connection: Connection, event_id: int) -> Sequence[Row]:
    """The event's versions, the earliest first, each with the report that stated it - the withdrawn ones too."""
    statement = (
        select(
            EventState.id,
            EventState.status,
            EventState.valid_from,
            EventState.recorded_at,
            EventState.candidate_id,
            EventState.added_by_id,
            EventState.withdrawn_by_id,
            EventCandidate.title.label("report_title"),
            EventCandidate.time_note,
            Article.id.label("article_id"),
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
        )
        .outerjoin(EventCandidate, EventCandidate.id == EventState.candidate_id)
        .outerjoin(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventState.event_id == event_id)
        .order_by(EventState.valid_from, EventState.id)
    )
    return connection.execute(statement).all()


def latest(connection: Connection, limit: int) -> Sequence[Row]:
    """The most recently reported events with their number of reports and of sources."""
    statement = _listed().order_by(Event.last_reported_at.desc(), Event.id.desc()).limit(limit)
    return connection.execute(statement).all()


def listed(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """These events as the list shows them; a merged one is left out."""
    return connection.execute(_listed().where(Event.id.in_(list(event_ids)))).all()


def _listed():
    """The events with their state now and their number of reports and of sources; a merged event has none."""
    reports = (
        select(
            EventCandidate.event_id,
            func.count().label("report_count"),
            func.count(func.distinct(func.coalesce(Source.name, Article.source_name))).label("source_count"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.event_id.is_not(None))
        .group_by(EventCandidate.event_id)
        .subquery()
    )
    return (
        select(Event.__table__, _CURRENT_STATE, reports.c.report_count, reports.c.source_count)
        .join(reports, reports.c.event_id == Event.id)
        .where(Event.merged_into_id.is_(None))
    )


def count(connection: Connection) -> int:
    """The events, without the merged ones."""
    return connection.execute(select(func.count()).select_from(Event).where(Event.merged_into_id.is_(None))).scalar_one()


def titles(connection: Connection, event_ids: Iterable[int]) -> dict[int, str]:
    statement = select(Event.id, Event.title).where(Event.id.in_(list(event_ids)))
    return {row.id: row.title for row in connection.execute(statement)}


def get(connection: Connection, event_id: int) -> Row | None:
    return connection.execute(select(Event.__table__, _CURRENT_STATE).where(Event.id == event_id)).one_or_none()


def reports(connection: Connection, event_id: int) -> Sequence[Row]:
    """The event's reports, the earliest first: the candidate, its article and source, and how it came to the event."""
    statement = (
        select(
            EventCandidate.id,
            EventCandidate.title,
            EventCandidate.status,
            EventCandidate.assignment_method,
            EventCandidate.assignment_score,
            Article.id.label("article_id"),
            Article.title.label("article_title"),
            Article.url.label("article_url"),
            _MOMENT.label("moment"),
            func.coalesce(Source.name, Article.source_name).label("source_display_name"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.event_id == event_id)
        .order_by(_MOMENT, EventCandidate.id)
    )
    return connection.execute(statement).all()


def for_correction(connection: Connection, event_ids: Iterable[int]) -> dict[int, Row]:
    """What a correction checks of the events: their name, the report they are named after, whether merged."""
    statement = select(Event.id, Event.title, Event.founding_candidate_id, Event.merged_into_id).where(
        Event.id.in_(list(event_ids))
    )
    return {row.id: row for row in connection.execute(statement)}


def assigned_reports(connection: Connection, event_id: int) -> Sequence[Row]:
    """The event's reports in the order they were assigned, with what they state and when: what a correction weighs
    again and names a new event by."""
    statement = (
        select(
            EventCandidate.id,
            EventCandidate.title,
            EventCandidate.event_type,
            EventCandidate.country,
            EventCandidate.status,
            _MOMENT.label("moment"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .where(EventCandidate.event_id == event_id)
        .order_by(EventCandidate.assigned_at, EventCandidate.id)
    )
    return connection.execute(statement).all()


def refresh_reach(connection: Connection, event_id: int) -> None:
    """The event's first and latest report as its reports stand after a correction moved some."""
    reach = (
        select(func.min(_MOMENT).label("first"), func.max(_MOMENT).label("last"))
        .select_from(EventCandidate)
        .join(Article, Article.id == EventCandidate.article_id)
        .where(EventCandidate.event_id == event_id)
    )
    first, last = connection.execute(reach).one()
    if first is not None:
        connection.execute(update(Event).where(Event.id == event_id).values(first_reported_at=first, last_reported_at=last))


def states_in_effect(connection: Connection, event_id: int) -> Sequence[Row]:
    """The event's versions not withdrawn whose report is there: what a correction compares its replay with. One whose
    report is gone stays as it is."""
    statement = (
        select(EventState.id, EventState.status, EventState.valid_from, EventState.candidate_id)
        .where(EventState.event_id == event_id, _NOT_WITHDRAWN, EventState.candidate_id.is_not(None))
        .order_by(EventState.id)
    )
    return connection.execute(statement).all()
