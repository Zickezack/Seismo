"""Data access for the events (Ereignisse) and the assignment of event candidates to them."""
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, Row, exists, func, insert, literal, or_, select, text, update
from sqlalchemy.orm import aliased

from app.db.models import (
    Article,
    CorrectionKind,
    Entity,
    EntityAlias,
    EntityKind,
    EntityMention,
    Event,
    EventCandidate,
    EventCorrection,
    EventState,
    Evidence,
    Extraction,
    Instrument,
    Job,
    Source,
    Statement,
    WatchItemKind,
    WatchlistItem,
)

# Of the two-key advisory lock that serialises the assignments (7301 LLM slots, 7302 duplicate checks).
EVENT_LOCK_NAMESPACE = 7303

# When a report was made: its article's publication, else its fetch.
_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)
# The version of an event's state in effect at a moment: the latest from before it, of equal times the one recorded last.
# A version a correction withdrew is never in effect.
_IN_EFFECT_ORDER = (EventState.valid_from.desc(), EventState.id.desc())
_NOT_WITHDRAWN = EventState.withdrawn_by_id.is_(None)
# A report of its article's reading a reprocessing replaced (reprocessing_service): never assigned; until it leaves its
# event, the one its successor finds it by.
_REPLACED = exists().where(Extraction.id == EventCandidate.extraction_id, Extraction.replaced_by_id.is_not(None))
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
    """Candidates without an event and never queued, the earliest report first; none of a replaced reading."""
    statement = (
        select(EventCandidate.id)
        .join(Article, Article.id == EventCandidate.article_id)
        .where(
            EventCandidate.event_id.is_(None),
            ~_REPLACED,
            # Any job of it, failed ones too: a failed one comes back only when the admin requeues it.
            ~exists().where(Job.dedupe_key == func.concat(literal(f"{job_kind}:"), EventCandidate.id)),
        )
        .order_by(_MOMENT, EventCandidate.id)
        .limit(limit)
    )
    return list(connection.execute(statement).scalars())


def count_unassigned(connection: Connection) -> int:
    statement = select(func.count()).select_from(EventCandidate).where(EventCandidate.event_id.is_(None), ~_REPLACED)
    return connection.execute(statement).scalar_one()


def get_for_assignment(connection: Connection, candidate_id: int) -> Row | None:
    """What a candidate is compared by, its event if it has one, when it was reported, the state it reports, and
    whether its reading was replaced."""
    statement = (
        select(
            EventCandidate.id,
            EventCandidate.extraction_id,
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
            _REPLACED.label("replaced"),
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
    by, and whether their reading was replaced."""
    statement = (
        select(
            EventCandidate.event_id,
            EventCandidate.article_id,
            EventCandidate.title,
            EventCandidate.summary,
            EventCandidate.place,
            EventCandidate.actors,
            _REPLACED.label("replaced"),
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


@dataclass(frozen=True)
class EventFilter:
    """What the event list and the start page are narrowed to; an empty field narrows nothing. An event has a country,
    a type, a sector, a company, an impact or a horizon when it or one of its reports states it."""

    country: str | None = None
    event_type: str | None = None
    sector_id: int | None = None
    company: str | None = None  # a part of a name form of a company or subsidiary its reports name, any case
    min_sources: int | None = None
    impact_kinds: tuple[str, ...] = ()  # a report names an impact on one of these kinds of target
    horizon: str | None = None  # a report names an impact of this horizon
    since: datetime | None = None  # a report from then on
    development: str | None = None  # with `since`: NEW, DEVELOPING or CHANGED
    watchlist_id: int | None = None  # it has one of the things the watchlist follows (_watched)


# First reported from `since` on; reported before and again from then on; a change of state from then on.
NEW, DEVELOPING, CHANGED = "new", "developing", "changed"
NO_FILTER = EventFilter()


def latest(connection: Connection, limit: int, criteria: EventFilter = NO_FILTER) -> Sequence[Row]:
    """The most recently reported events that match, with their number of reports and of sources."""
    statement = _listed(criteria).order_by(Event.last_reported_at.desc(), Event.id.desc()).limit(limit)
    return connection.execute(statement).all()


def listed(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """These events as the list shows them; a merged one is left out."""
    return connection.execute(_listed().where(Event.id.in_(list(event_ids)))).all()


def _listed(criteria: EventFilter = NO_FILTER):
    """The events that match with their state now and their number of reports and of sources; a merged event has
    none."""
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
        .where(Event.merged_into_id.is_(None), *_matching(criteria, reports.c.source_count))
    )


def _matching(criteria: EventFilter, source_count) -> list:
    """The conditions on an event of `criteria`, in the query: the list shows only the latest, so a filter in the
    browser would miss the rest."""
    conditions = []
    if criteria.country:
        conditions.append(or_(Event.country == criteria.country, _a_report(EventCandidate.country == criteria.country)))
    if criteria.event_type:
        conditions.append(or_(Event.event_type == criteria.event_type, _a_report(EventCandidate.event_type == criteria.event_type)))
    if criteria.sector_id is not None:
        conditions.append(_a_report(EventCandidate.id.in_(_naming(EntityAlias.entity_id == criteria.sector_id))))
    if criteria.company:
        name_form = aliased(EntityAlias)
        named = exists().where(
            name_form.entity_id == Entity.id, func.strpos(func.lower(name_form.value), criteria.company.lower()) > 0
        )
        companies = Entity.kind.in_((EntityKind.COMPANY, EntityKind.SUBSIDIARY))
        conditions.append(_a_report(EventCandidate.id.in_(_naming(companies, named))))
    if criteria.min_sources:
        conditions.append(source_count >= criteria.min_sources)
    if criteria.impact_kinds:
        conditions.append(_a_report(or_(*(EventCandidate.impacts.contains([{"target_kind": kind}]) for kind in criteria.impact_kinds))))
    if criteria.horizon:
        conditions.append(_a_report(EventCandidate.impacts.contains([{"horizon": criteria.horizon}])))
    if criteria.since is not None:
        recent = select(EventCandidate.id).join(Article, Article.id == EventCandidate.article_id).where(
            EventCandidate.event_id == Event.id, _MOMENT >= criteria.since
        )
        conditions += [Event.last_reported_at >= criteria.since, recent.exists()]
        if criteria.development == NEW:
            conditions.append(Event.first_reported_at >= criteria.since)
        elif criteria.development == DEVELOPING:
            conditions.append(Event.first_reported_at < criteria.since)
        elif criteria.development == CHANGED:
            conditions.append(_changed_since(criteria.since))
    if criteria.watchlist_id is not None:
        conditions.append(_watched(criteria.watchlist_id))
    return conditions


def _watched(watchlist_id: int):
    """The event has one of the things the watchlist follows: it is one of its events, has one of its types or
    countries - it or one of its reports -, or a report names one of its entities, or the one a Wertpapier of it is
    linked to. An entity or event merged since stands for the one it went into; an empty watchlist matches nothing."""
    item = WatchlistItem.watchlist_id == watchlist_id

    def values(kind: str):
        return select(WatchlistItem.value).where(item, WatchlistItem.kind == kind)

    chosen = select(WatchlistItem.entity_id.label("id")).where(item, WatchlistItem.kind == WatchItemKind.ENTITY).union(
        select(Instrument.entity_id).join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id).where(
            item, WatchlistItem.kind == WatchItemKind.INSTRUMENT, Instrument.entity_id.is_not(None)
        )
    )
    entities = _followed(Entity, select(Entity.id, Entity.merged_into_id).where(Entity.id.in_(chosen)))
    events = _followed(Event, select(Event.id, Event.merged_into_id).where(
        Event.id.in_(select(WatchlistItem.event_id).where(item, WatchlistItem.kind == WatchItemKind.EVENT))
    ))
    types, countries = values(WatchItemKind.EVENT_TYPE), values(WatchItemKind.COUNTRY)
    return or_(
        Event.id.in_(events),
        Event.event_type.in_(types),
        Event.country.in_(countries),
        _a_report(or_(
            EventCandidate.event_type.in_(types),
            EventCandidate.country.in_(countries),
            EventCandidate.id.in_(_naming(EntityAlias.entity_id.in_(entities))),
        )),
    )


def _followed(model, chosen):
    """The ids of the chosen rows and of those they were merged into, and so on: the mentions of a merged entity name
    the one it went into, the reports of a merged event are in the one it went into."""
    chain = chosen.cte(recursive=True)
    chain = chain.union(select(model.id, model.merged_into_id).join(chain, model.id == chain.c.merged_into_id))
    return select(chain.c.id)


def matching_ids(criteria: EventFilter, reported_since: datetime):
    """The ids of the events, not merged ones, reported from `reported_since` on that match: what the alert rules
    look at (alert_repository)."""
    source_count = (
        select(func.count(func.distinct(func.coalesce(Source.name, Article.source_name))))
        .select_from(EventCandidate)
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.event_id == Event.id)
        .scalar_subquery()
    )
    return select(Event.id).where(
        Event.merged_into_id.is_(None), Event.last_reported_at >= reported_since, *_matching(criteria, source_count)
    )


def split_off(event_id_column):
    """The event was made by a split of the analyst: no new incident of the world."""
    return exists().where(EventCorrection.event_id == event_id_column, EventCorrection.kind == CorrectionKind.SPLIT)


def _a_report(*conditions):
    """One of the event's reports meets the conditions."""
    return exists().where(EventCandidate.event_id == Event.id, *conditions)


def _naming(*conditions):
    """The reports that name an entity meeting the conditions, by any of its name forms."""
    return (
        select(EntityMention.candidate_id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .join(Entity, Entity.id == EntityAlias.entity_id)
        .where(*conditions)
    )


def _changed_since(since: datetime):
    """A change of the event's state from `since` on: a version not withdrawn whose status differs from the one
    before it - the first founds the state and changes none; a repeat is none (dashboard_service.changes_since)."""
    before = func.lag(EventState.status).over(partition_by=EventState.event_id, order_by=(EventState.valid_from, EventState.id))
    versions = select(EventState.event_id, EventState.status, EventState.valid_from, before.label("before")).where(
        _NOT_WITHDRAWN
    ).subquery()
    return exists().where(
        versions.c.event_id == Event.id,
        versions.c.valid_from >= since,
        versions.c.before.is_not(None),
        versions.c.status != versions.c.before,
    )


def countries(connection: Connection) -> list[str]:
    """The countries the events and their reports state, by code: what the filter offers."""
    reported = select(EventCandidate.country.label("country")).where(
        EventCandidate.event_id.is_not(None), EventCandidate.country.is_not(None)
    )
    founded = select(Event.country.label("country")).where(Event.merged_into_id.is_(None), Event.country.is_not(None))
    both = reported.union(founded).subquery()
    return list(connection.execute(select(both.c.country).order_by(both.c.country)).scalars())


def active_since(connection: Connection, since: datetime, criteria: EventFilter = NO_FILTER) -> Sequence[Row]:
    """The events, not merged ones, with a report from `since` on that match: each with its state now, its reports and
    sources in all and those from `since` on."""
    source = func.coalesce(Source.name, Article.source_name)
    recent = _MOMENT >= since
    reports = (
        select(
            EventCandidate.event_id,
            func.count().label("report_count"),
            func.count(func.distinct(source)).label("source_count"),
            func.count().filter(recent).label("recent_reports"),
            func.count(func.distinct(source)).filter(recent).label("recent_sources"),
        )
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.event_id.is_not(None))
        .group_by(EventCandidate.event_id)
        .subquery()
    )
    statement = (
        select(Event.__table__, _CURRENT_STATE, reports.c.report_count, reports.c.source_count, reports.c.recent_reports,
               reports.c.recent_sources)
        .join(reports, reports.c.event_id == Event.id)
        .where(
            Event.merged_into_id.is_(None), Event.last_reported_at >= since, reports.c.recent_reports > 0,
            *_matching(criteria, reports.c.source_count),
        )
    )
    return connection.execute(statement).all()


def versions_of(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The versions not withdrawn of these events, each event's the earliest first: what tells a change from a
    repeat (event_service.mark_repeats)."""
    statement = (
        select(EventState.event_id, EventState.status, EventState.valid_from)
        .where(EventState.event_id.in_(list(event_ids)), _NOT_WITHDRAWN)
        .order_by(EventState.event_id, EventState.valid_from, EventState.id)
    )
    return connection.execute(statement).all()


def statements_of(connection: Connection, event_id: int) -> Sequence[Row]:
    """The statements of the event's reports with their quotes, each report's in order: the Chronik's evidence."""
    statement = (
        select(
            Statement.candidate_id, Statement.kind, Statement.text, Statement.extraction_confidence, Evidence.quote,
            Evidence.source_quality,
        )
        .join(EventCandidate, EventCandidate.id == Statement.candidate_id)
        .join(Evidence, Evidence.statement_id == Statement.id)
        .where(EventCandidate.event_id == event_id)
        .order_by(Statement.candidate_id, Statement.position)
    )
    return connection.execute(statement).all()


def count(connection: Connection, criteria: EventFilter = NO_FILTER) -> int:
    """The events that match, without the merged ones."""
    return connection.execute(select(func.count()).select_from(_listed(criteria).subquery())).scalar_one()


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
            EventCandidate.article_id,
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


def name_after(connection: Connection, event_id: int, report: Row) -> None:
    """The event is named after the report from now on: its title, type and country."""
    values = {"title": report.title, "event_type": report.event_type, "country": report.country, "founding_candidate_id": report.id}
    connection.execute(update(Event).where(Event.id == event_id).values(values))


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
