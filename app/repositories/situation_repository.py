"""Data access for the Lagen, the events that belong to them and when each event was last weighed against them."""
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import Connection, Row, exists, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import (
    Article,
    EntityAlias,
    EntityMention,
    Event,
    EventCandidate,
    MemberStatus,
    Situation,
    SituationCheck,
    SituationMember,
    SituationStatus,
    Source,
)
from app.repositories.source_repository import NAME_COLLATION

# Of the two-key advisory lock that serialises the forming of Lagen (7301 LLM slots, 7302 duplicate checks, 7303
# assignments, 7304 entities, 7305 alert sweeps).
SITUATION_LOCK_NAMESPACE = 7306

# The belongings in effect: an event counts for the Lage.
IN_EFFECT = (MemberStatus.AUTOMATIC, MemberStatus.CONFIRMED)
# Those not removed: of a proposed Lage, and the resumptions of a dormant one, too.
LIVE = (MemberStatus.PROPOSED, MemberStatus.AUTOMATIC, MemberStatus.CONFIRMED)
_MOMENT = func.coalesce(Article.published_at, Article.fetched_at)


def lock(connection: Connection) -> None:
    """Until the end of the transaction no other one forms or corrects Lagen."""
    statement = text("SELECT pg_advisory_xact_lock(CAST(:namespace AS integer), 0)")
    connection.execute(statement, {"namespace": SITUATION_LOCK_NAMESPACE})


def database_now(connection: Connection) -> datetime:
    return connection.execute(select(func.now())).scalar_one()


# What the comparison reads.

def events_since(connection: Connection, since: datetime) -> Sequence[Row]:
    """The events, not merged ones, reported from `since` on."""
    statement = select(Event.id, Event.title, Event.first_reported_at, Event.last_reported_at).where(
        Event.merged_into_id.is_(None), Event.last_reported_at >= since
    )
    return connection.execute(statement).all()


def events(connection: Connection, event_ids: Iterable[int]) -> dict[int, Row]:
    """These events, not merged ones, with their latest report's summary."""
    latest = (
        select(EventCandidate.summary)
        .join(Article, Article.id == EventCandidate.article_id)
        .where(EventCandidate.event_id == Event.id)
        .order_by(_MOMENT.desc(), EventCandidate.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    statement = select(
        Event.id, Event.title, Event.first_reported_at, Event.last_reported_at, latest.label("latest_summary")
    ).where(Event.id.in_(list(event_ids)), Event.merged_into_id.is_(None))
    return {row.id: row for row in connection.execute(statement)}


def reports_of(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """What the comparison reads of the events' reports: their words."""
    statement = select(
        EventCandidate.event_id, EventCandidate.title, EventCandidate.summary, EventCandidate.place, EventCandidate.actors
    ).where(EventCandidate.event_id.in_(list(event_ids)))
    return connection.execute(statement).all()


def entities_of(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The entities the events' reports name, once per mention."""
    statement = (
        select(EventCandidate.event_id, EntityAlias.entity_id)
        .join(EntityMention, EntityMention.candidate_id == EventCandidate.id)
        .join(EntityAlias, EntityAlias.id == EntityMention.alias_id)
        .where(EventCandidate.event_id.in_(list(event_ids)))
    )
    return connection.execute(statement).all()


def situations(connection: Connection, statuses: Iterable[str]) -> Sequence[Row]:
    """The Lagen of these statuses, each with the latest report time of its events in effect."""
    latest = (
        select(func.max(Event.last_reported_at))
        .join(SituationMember, SituationMember.event_id == Event.id)
        .where(SituationMember.situation_id == Situation.id, SituationMember.status.in_(IN_EFFECT))
        .scalar_subquery()
    )
    statement = select(Situation.__table__, latest.label("last_event_at")).where(Situation.status.in_(list(statuses)))
    return connection.execute(statement).all()


def members(connection: Connection, situation_ids: Iterable[int] | None = None) -> Sequence[Row]:
    """Every belonging - of these Lagen, or of all -, removed ones too."""
    statement = select(SituationMember.situation_id, SituationMember.event_id, SituationMember.status)
    if situation_ids is not None:
        statement = statement.where(SituationMember.situation_id.in_(list(situation_ids)))
    return connection.execute(statement).all()


def due(connection: Connection, since: datetime) -> list[int]:
    """The events reported from `since` on to weigh against the Lagen: never weighed, or since then with a new report,
    or a Lage confirmed since."""
    checked = SituationCheck.checked_at
    newer_report = exists().where(EventCandidate.event_id == Event.id, EventCandidate.assigned_at > checked)
    confirmed_since = exists().where(Situation.status == SituationStatus.CONFIRMED, Situation.decided_at > checked)
    statement = (
        select(Event.id)
        .outerjoin(SituationCheck, SituationCheck.event_id == Event.id)
        .where(
            Event.merged_into_id.is_(None), Event.last_reported_at >= since,
            or_(checked.is_(None), newer_report, confirmed_since),
        )
        .order_by(Event.last_reported_at.desc(), Event.id)
    )
    return list(connection.execute(statement).scalars())


def record_checks(connection: Connection, event_ids: Iterable[int]) -> None:
    event_ids = sorted(set(event_ids))
    if not event_ids:
        return
    statement = pg_insert(SituationCheck).values([{"event_id": each} for each in event_ids])
    connection.execute(statement.on_conflict_do_update(index_elements=[SituationCheck.event_id], set_={"checked_at": func.now()}))


# What a sweep, a job and the analyst write.

def insert_situation(connection: Connection, name: str, description: str | None, status: str) -> int:
    values = {"name": name, "description": description, "status": status}
    if status != SituationStatus.PROPOSED:
        values["decided_at"] = func.now()
    return connection.execute(insert(Situation).values(values).returning(Situation.id)).scalar_one()


def add_member(connection: Connection, situation_id: int, event_id: int, status: str, origin: str, score: float | None) -> bool:
    """False when the event has a belonging to the Lage already, a removed one too: the analyst's decision stays."""
    statement = (
        pg_insert(SituationMember)
        .values(situation_id=situation_id, event_id=event_id, status=status, origin=origin, score=score)
        .on_conflict_do_nothing()
        .returning(SituationMember.id)
    )
    return connection.execute(statement).first() is not None


def set_member(connection: Connection, situation_id: int, event_id: int, status: str, origin: str) -> None:
    """The analyst's decision, over whatever stood before."""
    statement = pg_insert(SituationMember).values(
        situation_id=situation_id, event_id=event_id, status=status, origin=origin, decided_at=func.now()
    )
    statement = statement.on_conflict_do_update(
        index_elements=[SituationMember.situation_id, SituationMember.event_id],
        set_={"status": status, "origin": origin, "decided_at": func.now(), "moved_to_id": None},
    )
    connection.execute(statement)


def decide_members(connection: Connection, situation_id: int, event_ids: Iterable[int], status: str, from_statuses: Iterable[str],
                   moved_to_id: int | None = None) -> int:
    statement = (
        update(SituationMember)
        .where(SituationMember.situation_id == situation_id, SituationMember.event_id.in_(list(event_ids)),
               SituationMember.status.in_(list(from_statuses)))
        .values(status=status, decided_at=func.now(), moved_to_id=moved_to_id)
    )
    return connection.execute(statement).rowcount


def lock_situations(connection: Connection, situation_ids: Iterable[int]) -> dict[int, Row]:
    statement = select(Situation.__table__).where(Situation.id.in_(list(situation_ids))).order_by(Situation.id).with_for_update()
    return {row.id: row for row in connection.execute(statement)}


def set_status(connection: Connection, situation_ids: Iterable[int], status: str, merged_into_id: int | None = None) -> None:
    values = {"status": status, "decided_at": func.now(), "updated_at": func.now()}
    if merged_into_id is not None:
        values["merged_into_id"] = merged_into_id
    connection.execute(update(Situation).where(Situation.id.in_(list(situation_ids))).values(values))


def describe(connection: Connection, situation_id: int, name: str, description: str | None) -> bool:
    statement = update(Situation).where(Situation.id == situation_id).values(name=name, description=description, updated_at=func.now())
    return connection.execute(statement).rowcount == 1


def live_members(connection: Connection, situation_id: int) -> Sequence[Row]:
    statement = select(SituationMember.__table__).where(
        SituationMember.situation_id == situation_id, SituationMember.status.in_(LIVE)
    )
    return connection.execute(statement).all()


# What the pages show.

def get(connection: Connection, situation_id: int) -> Row | None:
    return connection.execute(select(Situation.__table__).where(Situation.id == situation_id)).one_or_none()


def listing(connection: Connection, statuses: Iterable[str], situation_id: int | None = None) -> Sequence[Row]:
    """The Lagen of these statuses, or the one, by name, each with its events by status and its latest event's report
    time."""
    def counted(*statuses: str):
        return (
            select(func.count())
            .where(SituationMember.situation_id == Situation.id, SituationMember.status.in_(statuses))
            .scalar_subquery()
        )

    latest = (
        select(func.max(Event.last_reported_at))
        .join(SituationMember, SituationMember.event_id == Event.id)
        .where(SituationMember.situation_id == Situation.id, SituationMember.status.in_(IN_EFFECT))
        .scalar_subquery()
    )
    statement = (
        select(
            Situation.__table__,
            counted(*IN_EFFECT).label("event_count"),
            counted(MemberStatus.AUTOMATIC).label("automatic_count"),
            counted(MemberStatus.PROPOSED).label("proposed_count"),
            latest.label("last_event_at"),
        )
        .where(Situation.status.in_(list(statuses)))
        .order_by(Situation.name.collate(NAME_COLLATION), Situation.id)
    )
    if situation_id is not None:
        statement = statement.where(Situation.id == situation_id)
    return connection.execute(statement).all()


def member_rows(connection: Connection, situation_ids: Iterable[int]) -> Sequence[Row]:
    """The belongings of these Lagen with their event's title and reach, the earliest event first."""
    statement = (
        select(
            SituationMember.situation_id, SituationMember.event_id, SituationMember.status, SituationMember.origin,
            SituationMember.score, SituationMember.added_at, SituationMember.decided_at, SituationMember.moved_to_id,
            Event.title, Event.first_reported_at, Event.last_reported_at, Event.merged_into_id,
        )
        .join(Event, Event.id == SituationMember.event_id)
        .where(SituationMember.situation_id.in_(list(situation_ids)))
        .order_by(Event.first_reported_at, Event.id)
    )
    return connection.execute(statement).all()


def of_event(connection: Connection, event_id: int) -> Sequence[Row]:
    """The Lagen, confirmed or proposed, the event belongs to, not removed."""
    statement = (
        select(Situation.id, Situation.name, Situation.status.label("situation_status"), SituationMember.status)
        .join(SituationMember, SituationMember.situation_id == Situation.id)
        .where(
            SituationMember.event_id == event_id, SituationMember.status.in_(LIVE),
            Situation.status.in_((SituationStatus.CONFIRMED, SituationStatus.PROPOSED)),
        )
        .order_by(Situation.name.collate(NAME_COLLATION))
    )
    return connection.execute(statement).all()


def choices(connection: Connection) -> Sequence[Row]:
    """The confirmed Lagen by name: what a form offers to choose from."""
    statement = select(Situation.id, Situation.name).where(Situation.status == SituationStatus.CONFIRMED).order_by(
        Situation.name.collate(NAME_COLLATION), Situation.id
    )
    return connection.execute(statement).all()


def sources_of(connection: Connection, event_ids: Iterable[int]) -> Sequence[Row]:
    """The sources that reported these events, each with how many of them, the most first."""
    source = func.coalesce(Source.name, Article.source_name)
    statement = (
        select(source.label("name"), func.count(func.distinct(EventCandidate.event_id)).label("events"))
        .join(Article, Article.id == EventCandidate.article_id)
        .outerjoin(Source, Source.id == Article.source_id)
        .where(EventCandidate.event_id.in_(list(event_ids)))
        .group_by(source)
        .order_by(func.count(func.distinct(EventCandidate.event_id)).desc(), source)
    )
    return connection.execute(statement).all()
