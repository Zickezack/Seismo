"""Lagen (userstories.md, Ereignisidentität und zeitliche Entwicklung, story 4): related events over weeks - a war, a
trade conflict, a political crisis, a series of attacks - grouped a level above the Ereignis, which stays one incident
(the admin's decision of 2026-10-24). An event may belong to several Lagen.

Compared by their words and the entities they name: an event is the TF-IDF vector of the words of its reports (as
event_service weighs a report) and of its reports' entities, each mention counting ENTITY_WEIGHT words - a rare entity
weighs most, as a rare word does. A Lage is the sum of its events', as a unit vector: its profile. Measured on the
dev data (2026-11-03), scores alone do not tell a Lage's events from others that share a country or a person (0.10 to
0.27 against 0.10 to 0.17 for a Lage of the war in Ukraine), so they only choose what the LLM is shown; only from
JOIN_FROM does a score assign alone.

The worker sweeps once an hour (`sweep`), under an advisory lock, within the hour's budget of LLM calls of the
Plattform-Einstellungen, behind live work:

- An event reported within the open period (Plattform-Einstellung) is weighed against the confirmed Lagen when it is
  new, has a new report, or a Lage was confirmed since it was last weighed: from JOIN_FROM it joins a Lage, between
  ASK_FROM and JOIN_FROM the LLM decides among the up to CHOICES most similar - one, several or none. It joins as
  automatic, in effect and flagged until the analyst confirms it; a dormant Lage - no event within the open period -
  takes it as a proposed resumption instead, in effect only once the analyst confirms it.
- The events within the open period that belong to no Lage are grouped (`groups`): an event with the most others from
  LINK_FROM similarity and those others, at least MIN_EVENTS. The LLM is shown a group with the up to CHOICES most
  similar Lagen: the events belong to one of them, form a new Lage it names and describes in English, or form none -
  kept as dismissed, so the same events are not asked again: a group is asked only while at least half of its events
  were never in a proposal.

The analyst confirms or rejects a proposed Lage, confirms or removes its events, splits events off into a new Lage,
merges Lagen and adds an event (situation corrections below). A removed belonging stays, so the system never assigns
that event to that Lage again.
"""
import json
import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, Engine, Row

from app.db.models import MemberOrigin, MemberStatus, SituationStatus
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import event_repository, reprocessing_repository, situation_repository
from app.repositories.situation_repository import IN_EFFECT, LIVE
from app.services import dashboard_service, event_service, job_service, platform_settings_service
from app.services.bulk import BulkResult
from app.services.event_service import EventListItem, words

logger = logging.getLogger(__name__)

ASSIGN_KIND = job_service.ASSIGN_SITUATION_JOB_KIND
PROPOSE_KIND = job_service.PROPOSE_SITUATION_JOB_KIND
BUDGET_PERIOD = timedelta(hours=1)
JOIN_FROM = 0.4
ASK_FROM = 0.12
LINK_FROM = 0.2
MIN_EVENTS = 3
MAX_SHOWN = 12
CHOICES = 3
ENTITY_WEIGHT = 2
# A term most events share links nothing: "russia" in half of them would chain them all.
COMMON_SHARE = 0.25
SAMPLES = 5
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 1000
MAX_REASON_CHARS = 300
STATUS_LABELS = {
    SituationStatus.PROPOSED: "Vorgeschlagen", SituationStatus.CONFIRMED: "Bestätigt", SituationStatus.REJECTED: "Abgelehnt",
    SituationStatus.DISMISSED: "Verworfen", SituationStatus.MERGED: "Zusammengeführt",
}
MEMBER_LABELS = {
    MemberStatus.PROPOSED: "vorgeschlagen", MemberStatus.AUTOMATIC: "automatisch", MemberStatus.CONFIRMED: "bestätigt",
    MemberStatus.REMOVED: "entfernt",
}
ORIGIN_LABELS = {
    MemberOrigin.PROPOSAL: "Vorschlag", MemberOrigin.SCORE: "Ähnlichkeit", MemberOrigin.LLM: "LLM", MemberOrigin.ANALYST: "Analyst",
}

SITUATION_DEFINITION = (
    "A situation is a development over weeks or months that many single events belong to - a war, a trade conflict, "
    "a political or financial crisis, a series of attacks, an epidemic, a disaster and its aftermath. An event belongs "
    "to a situation only if it is a direct part of that development, not merely about the same country, person, "
    "company or kind of event."
)
ASSIGN_PROMPT = f"""You decide which ongoing situations a news event belongs to, for an event database. {SITUATION_DEFINITION}
You are given the event and known situations, each with some of its events. Answer with the numbers of the situations
the event belongs to - one, several or none - and a short reason."""
PROPOSE_PROMPT = f"""You group news events into situations, for an event database. {SITUATION_DEFINITION}
You are given a group of events that share words and actors, and possibly known situations. Decide:
- If the events, or most of them, belong to one of the known situations, give its number as "known" and list the
  numbers of those events.
- Otherwise, if at least three of them form one situation, set "situation" to true, name it in a few English words
  (e.g. "Russia-Ukraine war", "US-China trade conflict", "Houthi attacks on shipping in the Red Sea"), describe it in
  one or two English sentences, and list the numbers of the events that belong to it.
- If they form no situation, set "situation" to false and "known" to null."""


class UnusableAnswer(Exception):
    """The LLM's answer cannot be read: retried with the job."""


class InvalidSituation(ValueError):
    """An invalid entry for a Lage. The message is shown to the analyst as is."""


# Comparing.

def documents(reports: Iterable, mentions: Iterable) -> dict[int, Counter]:
    """Per event, the words of its reports and its reports' entities ("#<id>"), each mention ENTITY_WEIGHT words."""
    found: dict[int, Counter] = defaultdict(Counter)
    for report in reports:
        found[report.event_id].update(words(report))
    for mention in mentions:
        found[mention.event_id][f"#{mention.entity_id}"] += ENTITY_WEIGHT
    return found


def vectors(found: Mapping[int, Counter]) -> dict[int, dict[str, float]]:
    """The TF-IDF unit vectors of the events among themselves: a term counts the more, the fewer of them have it."""
    frequency = Counter(term for document in found.values() for term in document)
    count = len(found)

    def unit(document: Counter) -> dict[str, float]:
        vector = {term: (1 + math.log(n)) * math.log((count + 1) / frequency[term]) for term, n in document.items()}
        return _unit(vector)

    return {event_id: unit(document) for event_id, document in found.items()}


def profile(event_vectors: Iterable[dict[str, float]]) -> dict[str, float]:
    """A Lage's profile: the sum of its events' vectors, as a unit vector."""
    total: Counter = Counter()
    for vector in event_vectors:
        total.update(vector)
    return _unit(dict(total))


def similarity(one: Mapping[str, float], other: Mapping[str, float]) -> float:
    if len(one) > len(other):
        one, other = other, one
    return sum(value * other.get(term, 0.0) for term, value in one.items())


def _unit(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    return {term: value / norm for term, value in vector.items()} if norm else {}


def groups(event_vectors: Mapping[int, dict[str, float]], link: float = LINK_FROM, minimum: int = MIN_EVENTS) -> list[list[int]]:
    """Groups of similar events: the event with the most others from `link` similarity, with those others - the most
    similar first -, then the same among the rest, while a group has at least `minimum`. A term most events share is
    left out of the comparison."""
    by_term: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for event_id, vector in event_vectors.items():
        for term, value in vector.items():
            by_term[term].append((event_id, value))
    common = max(minimum, COMMON_SHARE * len(event_vectors))
    neighbours: dict[int, dict[int, float]] = {}
    for event_id, vector in event_vectors.items():
        scores: Counter = Counter()
        for term, value in vector.items():
            if len(by_term[term]) > common:
                continue
            for other, other_value in by_term[term]:
                if other != event_id:
                    scores[other] += value * other_value
        neighbours[event_id] = {other: score for other, score in scores.items() if score >= link}
    left = set(event_vectors)
    found = []
    while left:
        leader = max(left, key=lambda each: (sum(other in left for other in neighbours[each]), -each))
        near = sorted((other for other in neighbours[leader] if other in left), key=lambda other: (-neighbours[leader][other], other))
        if len(near) + 1 < minimum:
            break
        group = [leader, *near]
        found.append(group)
        left -= set(group)
    return found


@dataclass(frozen=True)
class Placement:
    """Where the scores put an event: the Lagen it joins by them, those the LLM decides among."""

    joins: list[tuple[int, float]]
    asks: list[tuple[int, float]]


def place(scores: Mapping[int, float]) -> Placement:
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    joins = [(situation_id, score) for situation_id, score in ranked if score >= JOIN_FROM]
    asks = [(situation_id, score) for situation_id, score in ranked if ASK_FROM <= score < JOIN_FROM][:CHOICES]
    return Placement(joins, asks)


@dataclass(frozen=True)
class SweepResult:
    joined: int = 0
    asked: int = 0
    proposed: int = 0


@dataclass
class _State:
    """What a sweep or an effect knows: the events' vectors, the Lagen's rows, profiles and belongings."""

    now: datetime
    since: datetime
    vectors: dict[int, dict[str, float]]
    situations: dict[int, Row]
    profiles: dict[int, dict[str, float]]
    belongings: dict[int, dict[int, str]]  # event -> Lage -> status, removed ones too

    def dormant(self, situation_id: int) -> bool:
        last = self.situations[situation_id].last_event_at
        return last is not None and last < self.since

    def joining_status(self, situation_id: int) -> str:
        """How an event the system assigns joins the Lage: a proposed one, or a dormant one, as a proposal."""
        situation = self.situations[situation_id]
        if situation.status == SituationStatus.PROPOSED or self.dormant(situation_id):
            return MemberStatus.PROPOSED
        return MemberStatus.AUTOMATIC


def _state(connection: Connection, extra_event_ids: Iterable[int] = ()) -> _State:
    settings = platform_settings_service.load(connection)
    now = situation_repository.database_now(connection)
    since = now - timedelta(days=settings.situation_open_days)
    window = {row.id for row in situation_repository.events_since(connection, since)}
    rows = {row.id: row for row in situation_repository.situations(connection, (SituationStatus.CONFIRMED, SituationStatus.PROPOSED))}
    belongings: dict[int, dict[int, str]] = defaultdict(dict)
    for member in situation_repository.members(connection):
        belongings[member.event_id][member.situation_id] = member.status
    counted: dict[int, list[int]] = defaultdict(list)
    for event_id, by_situation in belongings.items():
        for situation_id, status in by_situation.items():
            if situation_id in rows and status in (LIVE if rows[situation_id].status == SituationStatus.PROPOSED else IN_EFFECT):
                counted[situation_id].append(event_id)
    event_ids = window | {each for members in counted.values() for each in members} | set(extra_event_ids)
    found = documents(situation_repository.reports_of(connection, event_ids), situation_repository.entities_of(connection, event_ids))
    event_vectors = vectors(found)
    profiles = {
        situation_id: profile(event_vectors[each] for each in members if each in event_vectors)
        for situation_id, members in counted.items()
    }
    return _State(now, since, event_vectors, rows, profiles, belongings)


def sweep(engine: Engine, llm_available: bool) -> SweepResult:
    """Weigh the due events against the confirmed Lagen and look for new ones; LLM jobs only while Ollama serves the
    model, within the hour's budget. Nothing while the budget is 0: the forming of Lagen is held."""
    with engine.begin() as connection:
        settings = platform_settings_service.load(connection)
        if settings.situation_llm_calls_per_hour <= 0:
            return SweepResult()
        situation_repository.lock(connection)
        state = _state(connection)
        left = 0
        if llm_available:
            left = settings.situation_llm_calls_per_hour - _jobs_since(connection)
        joined, asked, checked = 0, 0, []
        confirmed = [each for each, row in state.situations.items() if row.status == SituationStatus.CONFIRMED]
        for event_id in situation_repository.due(connection, state.since):
            vector = state.vectors.get(event_id)
            if not vector:
                checked.append(event_id)
                continue
            known = state.belongings.get(event_id, {})
            scores = {each: similarity(vector, state.profiles.get(each, {})) for each in confirmed if each not in known}
            placement = place(scores)
            for situation_id, score in placement.joins:
                if situation_repository.add_member(
                    connection, situation_id, event_id, state.joining_status(situation_id), MemberOrigin.SCORE, round(score, 3)
                ):
                    joined += 1
            if not placement.asks:
                checked.append(event_id)
            elif left > 0:
                payload = {"event_id": event_id, "situations": [[each, round(score, 3)] for each, score in placement.asks]}
                if job_service.enqueue(connection, ASSIGN_KIND, payload, dedupe_key=f"{ASSIGN_KIND}:{event_id}", priority=1):
                    left -= 1
                    asked += 1
            # Without budget it stays due and comes again within the next hour's.
        situation_repository.record_checks(connection, checked)
        proposed = _propose(connection, state, left) if left > 0 else 0
    if joined or asked or proposed:
        logger.info("Situations swept", extra={"joined": joined, "asked": asked, "proposed": proposed})
    return SweepResult(joined, asked, proposed)


def _propose(connection: Connection, state: _State, left: int) -> int:
    """Queue a proposal for each group of events that belong to no Lage, as many as `left`."""
    taken = {
        event_id for event_id, by_situation in state.belongings.items()
        if any(each in state.situations and status in LIVE for each, status in by_situation.items())
    }
    asked_before = set(state.belongings) - taken
    window = {row.id for row in situation_repository.events_since(connection, state.since)}
    pool = {event_id: state.vectors[event_id] for event_id in window - taken if state.vectors.get(event_id)}
    queued = 0
    for group in groups(pool):
        if queued >= left:
            break
        if 2 * sum(each in asked_before for each in group) > len(group):
            continue
        shown = group[:MAX_SHOWN]
        payload = {"event_ids": shown}
        key = f"{PROPOSE_KIND}:{','.join(map(str, sorted(shown)))}"
        if job_service.enqueue(connection, PROPOSE_KIND, payload, dedupe_key=key, priority=1):
            queued += 1
    return queued


def _near(state: _State, event_ids: list[int]) -> list[int]:
    """The up to CHOICES Lagen, confirmed or proposed, most similar to the events together, from ASK_FROM."""
    centre = profile(state.vectors[each] for each in event_ids if each in state.vectors)
    near = sorted(((similarity(centre, vector), each) for each, vector in state.profiles.items()), key=lambda item: (-item[0], item[1]))
    return [each for score, each in near[:CHOICES] if score >= ASK_FROM]


def _jobs_since(connection: Connection) -> int:
    return reprocessing_repository.jobs_since(connection, (ASSIGN_KIND, PROPOSE_KIND), BUDGET_PERIOD)


# Asking the LLM.

def assign(engine: Engine, payload: Mapping, client: OllamaClient, limiter: LlmCallLimiter) -> Callable[[Connection], None] | None:
    """First phase of the job "Lage zuordnen": ask which of the offered Lagen the event belongs to. The effect adds it
    to those still confirmed, and records it as weighed."""
    event_id = payload["event_id"]
    scores = {situation_id: score for situation_id, score in payload["situations"]}
    with engine.connect() as connection:
        event = situation_repository.events(connection, [event_id]).get(event_id)
        rows = {row.id: row for row in situation_repository.situations(connection, (SituationStatus.CONFIRMED,))}
        offered = [rows[each] for each in scores if each in rows]
        samples = _samples(connection, [each.id for each in offered])
    if event is None:
        return None
    chosen: list[int] = []
    if offered:
        with limiter.slot():
            answer = client.chat_json(assign_messages(event, offered, samples), assign_schema(len(offered)))
        chosen = [offered[number - 1].id for number in chosen_numbers(answer.content if answer.complete else "", len(offered))]

    def store(connection: Connection) -> None:
        situation_repository.lock(connection)
        state = _state(connection, [event_id])
        for situation_id in chosen:
            row = state.situations.get(situation_id)
            if row is None or row.status != SituationStatus.CONFIRMED:
                continue
            situation_repository.add_member(
                connection, situation_id, event_id, state.joining_status(situation_id), MemberOrigin.LLM, scores[situation_id]
            )
        situation_repository.record_checks(connection, [event_id])
        logger.info("Situations weighed", extra={"event_id": event_id, "offered": len(offered), "chosen": len(chosen)})

    return store


def propose(engine: Engine, payload: Mapping, client: OllamaClient, limiter: LlmCallLimiter) -> Callable[[Connection], None] | None:
    """First phase of the job "Lage vorschlagen": ask whether the group forms a Lage - a known one or a new one. The
    known ones are those most similar when the job runs, not when it was queued: the proposals of one sweep run one
    after the other, so a later one is shown the Lage an earlier one proposed (on the dev data, 2026-11-03, two
    groups of the war in Ukraine otherwise each proposed it). The effect adds the events to the known one, or stores
    the new one as proposed, or the group as dismissed; events that joined a Lage meanwhile are left out of a new
    one."""
    with engine.connect() as connection:
        found = situation_repository.events(connection, payload["event_ids"])
        shown = [found[each] for each in payload["event_ids"] if each in found]
        state = _state(connection)
        known = [state.situations[each] for each in _near(state, [each.id for each in shown])]
        samples = _samples(connection, [each.id for each in known])
    if len(shown) < MIN_EVENTS:
        return None
    with limiter.slot():
        answer = client.chat_json(propose_messages(shown, known, samples), propose_schema(len(shown), len(known)))
    decision = proposal(answer.content if answer.complete else "", len(shown), len(known))
    members = [shown[number - 1].id for number in decision.events]

    def store(connection: Connection) -> None:
        situation_repository.lock(connection)
        state = _state(connection)
        target = known[decision.known - 1].id if decision.known else None
        if target is not None and target in state.situations:
            for event_id in members:
                situation_repository.add_member(connection, target, event_id, state.joining_status(target), MemberOrigin.LLM, None)
            logger.info("Situation extended", extra={"situation_id": target, "events": len(members)})
            return
        free = [
            event_id for event_id in (members if decision.situation else [each.id for each in shown])
            if not any(each in state.situations and status in LIVE for each, status in state.belongings.get(event_id, {}).items())
        ]
        if decision.situation and len(free) >= MIN_EVENTS:
            status, name = SituationStatus.PROPOSED, decision.name
        else:
            # Kept, so the same events are not asked again; never shown as a Lage.
            status, name, free = SituationStatus.DISMISSED, decision.name or "-", [each.id for each in shown]
        situation_id = situation_repository.insert_situation(connection, name, decision.description or None, status)
        for event_id in free:
            situation_repository.add_member(connection, situation_id, event_id, MemberStatus.PROPOSED, MemberOrigin.PROPOSAL, None)
        logger.info("Situation proposed", extra={"situation_id": situation_id, "status": status, "events": len(free)})

    return store


def _samples(connection: Connection, situation_ids: list[int]) -> dict[int, list[str]]:
    """Per Lage, the titles of its latest events, what the LLM knows it by besides its name."""
    found: dict[int, list[Row]] = defaultdict(list)
    for row in situation_repository.member_rows(connection, situation_ids):
        if row.status in LIVE:
            found[row.situation_id].append(row)
    return {
        situation_id: [row.title for row in sorted(rows, key=lambda row: row.last_reported_at, reverse=True)[:SAMPLES]]
        for situation_id, rows in found.items()
    }


def _event_line(number: int, event) -> str:
    return (
        f"{number}. {event.title} (reported {event.first_reported_at.date().isoformat()} to "
        f"{event.last_reported_at.date().isoformat()})\n   {event.latest_summary or ''}"
    )


def _situation_lines(situations: list, samples: Mapping[int, list[str]]) -> str:
    return "\n".join(
        f"{number}. {situation.name}: {situation.description or ''}\n   Some of its events: " + "; ".join(samples.get(situation.id, []))
        for number, situation in enumerate(situations, start=1)
    )


def assign_messages(event, situations: list, samples: Mapping[int, list[str]]) -> list[dict]:
    content = f"Event:\n{_event_line(1, event)[3:]}\n\nKnown situations:\n{_situation_lines(situations, samples)}"
    return [{"role": "system", "content": ASSIGN_PROMPT}, {"role": "user", "content": content}]


def propose_messages(events: list, situations: list, samples: Mapping[int, list[str]]) -> list[dict]:
    content = "Events:\n" + "\n".join(_event_line(number, event) for number, event in enumerate(events, start=1))
    content += f"\n\nKnown situations:\n{_situation_lines(situations, samples) or 'none'}"
    return [{"role": "system", "content": PROPOSE_PROMPT}, {"role": "user", "content": content}]


def assign_schema(count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
            "belongs_to": {"type": "array", "items": {"enum": list(range(1, count + 1))}, "maxItems": count},
        },
        "required": ["reason", "belongs_to"],
        "additionalProperties": False,
    }


def propose_schema(events: int, situations: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
            "known": {"enum": [*range(1, situations + 1), None]},
            "situation": {"type": "boolean"},
            "name": {"type": "string", "maxLength": 120},
            "description": {"type": "string", "maxLength": 500},
            "events": {"type": "array", "items": {"enum": list(range(1, events + 1))}, "maxItems": events},
        },
        "required": ["reason", "known", "situation", "name", "description", "events"],
        "additionalProperties": False,
    }


def chosen_numbers(content: str, count: int) -> list[int]:
    """The numbers of the Lagen the LLM chose, each once, in order."""
    try:
        numbers = json.loads(content)["belongs_to"]
    except (ValueError, TypeError, KeyError):
        raise UnusableAnswer("Antwort ohne belongs_to") from None
    return _numbers(numbers, count, "belongs_to")


@dataclass(frozen=True)
class Proposal:
    situation: bool
    known: int | None
    name: str
    description: str
    events: list[int]


def proposal(content: str, events: int, situations: int) -> Proposal:
    try:
        answer = json.loads(content)
        known, situation, name, description = answer["known"], answer["situation"], answer["name"], answer["description"]
        numbers = answer["events"]
    except (ValueError, TypeError, KeyError):
        raise UnusableAnswer("Antwort ohne known, situation, name, description oder events") from None
    if known is not None and (type(known) is not int or not 1 <= known <= situations):
        raise UnusableAnswer(f"known ausserhalb von 1 bis {situations}")
    if type(situation) is not bool or not isinstance(name, str) or not isinstance(description, str):
        raise UnusableAnswer("situation, name oder description vom falschen Typ")
    return Proposal(
        situation, known, " ".join(name.split())[:MAX_NAME_CHARS], " ".join(description.split())[:MAX_DESCRIPTION_CHARS],
        _numbers(numbers, events, "events"),
    )


def _numbers(numbers, count: int, name: str) -> list[int]:
    if not isinstance(numbers, list) or any(type(each) is not int or not 1 <= each <= count for each in numbers):
        raise UnusableAnswer(f"{name} ausserhalb von 1 bis {count}")
    return list(dict.fromkeys(numbers))


# The analyst's decisions, each under the lock, all or nothing.

@dataclass(frozen=True)
class SplitResult:
    processed: int
    blocked: list[str]
    situation_id: int | None = None


def confirm(engine: Engine, situation_ids: Iterable[int]) -> BulkResult:
    """Bestätigen: the proposed Lagen are in effect, with their proposed events."""
    def action(connection: Connection, rows: dict[int, Row]) -> None:
        situation_repository.set_status(connection, rows, SituationStatus.CONFIRMED)
        for situation_id in rows:
            events = [row.event_id for row in situation_repository.live_members(connection, situation_id)]
            situation_repository.decide_members(connection, situation_id, events, MemberStatus.CONFIRMED, (MemberStatus.PROPOSED,))

    return _decide(engine, situation_ids, (SituationStatus.PROPOSED,), "nicht vorgeschlagen", action)


def reject(engine: Engine, situation_ids: Iterable[int]) -> BulkResult:
    """Ablehnen: the proposed Lagen are not; kept, so their events are not proposed together again."""
    return _decide(
        engine, situation_ids, (SituationStatus.PROPOSED,), "nicht vorgeschlagen",
        lambda connection, rows: situation_repository.set_status(connection, rows, SituationStatus.REJECTED),
    )


def _decide(engine: Engine, situation_ids: Iterable[int], allowed: tuple[str, ...], why: str, action) -> BulkResult:
    situation_ids = sorted(set(situation_ids))
    if not situation_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        situation_repository.lock(connection)
        rows = situation_repository.lock_situations(connection, situation_ids)
        blocked = [f"Lage #{each} (nicht gefunden)" for each in situation_ids if each not in rows]
        blocked += [f"{row.name} ({why})" for row in rows.values() if row.status not in allowed]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        action(connection, rows)
    return BulkResult(processed=len(situation_ids), blocked=[])


def confirm_events(engine: Engine, situation_id: int, event_ids: Iterable[int]) -> BulkResult:
    """Bestätigen: the selected automatic or proposed events belong to the Lage."""
    return _members(engine, situation_id, event_ids, lambda connection, ids: situation_repository.decide_members(
        connection, situation_id, ids, MemberStatus.CONFIRMED, (MemberStatus.AUTOMATIC, MemberStatus.PROPOSED)
    ))


def remove_events(engine: Engine, situation_id: int, event_ids: Iterable[int]) -> BulkResult:
    """Entfernen: the selected events no longer belong to the Lage, and the system never assigns them to it again."""
    return _members(engine, situation_id, event_ids, lambda connection, ids: situation_repository.decide_members(
        connection, situation_id, ids, MemberStatus.REMOVED, LIVE
    ))


def _members(engine: Engine, situation_id: int, event_ids: Iterable[int], action) -> BulkResult:
    event_ids = sorted(set(event_ids))
    if not event_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        situation_repository.lock(connection)
        row = situation_repository.lock_situations(connection, [situation_id]).get(situation_id)
        if row is None or row.status not in (SituationStatus.CONFIRMED, SituationStatus.PROPOSED):
            return BulkResult(processed=0, blocked=[f"Lage #{situation_id} (nicht gefunden oder nicht mehr in Kraft)"])
        live = {member.event_id for member in situation_repository.live_members(connection, situation_id)}
        blocked = [f"Ereignis {each} (gehört nicht zur Lage)" for each in event_ids if each not in live]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        action(connection, event_ids)
    return BulkResult(processed=len(event_ids), blocked=[])


def split(engine: Engine, situation_id: int, event_ids: Iterable[int], raw_name: str | None) -> SplitResult:
    """Als neue Lage abspalten: the selected events of the Lage become a new confirmed Lage of that name."""
    name = parse_name(raw_name)
    event_ids = sorted(set(event_ids))
    if not event_ids:
        return SplitResult(0, [])
    with engine.begin() as connection:
        situation_repository.lock(connection)
        row = situation_repository.lock_situations(connection, [situation_id]).get(situation_id)
        if row is None or row.status != SituationStatus.CONFIRMED:
            return SplitResult(0, [f"Lage #{situation_id} (nicht gefunden oder nicht bestätigt)"])
        live = {member.event_id for member in situation_repository.live_members(connection, situation_id)}
        blocked = [f"Ereignis {each} (gehört nicht zur Lage)" for each in event_ids if each not in live]
        if blocked:
            return SplitResult(0, blocked)
        new_id = situation_repository.insert_situation(connection, name, None, SituationStatus.CONFIRMED)
        for event_id in event_ids:
            situation_repository.set_member(connection, new_id, event_id, MemberStatus.CONFIRMED, MemberOrigin.ANALYST)
        situation_repository.decide_members(connection, situation_id, event_ids, MemberStatus.REMOVED, LIVE, moved_to_id=new_id)
    logger.info("Situation split", extra={"situation_id": situation_id, "into_id": new_id, "events": len(event_ids)})
    return SplitResult(len(event_ids), [], new_id)


def merge(engine: Engine, situation_id: int, other_ids: Iterable[int]) -> BulkResult:
    """In diese Lage zusammenführen: the events of the other confirmed Lagen go to this one - where it has decided on
    an event already, its decision stays -; each other one stays as merged into it."""
    other_ids = sorted(set(other_ids) - {situation_id})
    if not other_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        situation_repository.lock(connection)
        rows = situation_repository.lock_situations(connection, [situation_id, *other_ids])
        target = rows.get(situation_id)
        if target is None or target.status != SituationStatus.CONFIRMED:
            return BulkResult(processed=0, blocked=[f"Lage #{situation_id} (nicht gefunden oder nicht bestätigt)"])
        blocked = [f"Lage #{each} (nicht gefunden)" for each in other_ids if each not in rows]
        blocked += [f"{rows[each].name} (nicht bestätigt)" for each in other_ids if each in rows and rows[each].status != SituationStatus.CONFIRMED]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        for other_id in other_ids:
            members = situation_repository.live_members(connection, other_id)
            for member in members:
                situation_repository.add_member(connection, situation_id, member.event_id, member.status, member.origin, member.score)
            situation_repository.decide_members(
                connection, other_id, [member.event_id for member in members], MemberStatus.REMOVED, LIVE, moved_to_id=situation_id
            )
        situation_repository.set_status(connection, other_ids, SituationStatus.MERGED, merged_into_id=situation_id)
    logger.info("Situations merged", extra={"situation_id": situation_id, "merged": len(other_ids)})
    return BulkResult(processed=len(other_ids), blocked=[])


def add_event(engine: Engine, situation_id: int, event_id: int) -> str:
    """Zu Lage hinzufügen, from the event's page: the analyst's decision, also over a removal. Returns the Lage's name;
    InvalidSituation for a Lage not in effect or an event not found."""
    with engine.begin() as connection:
        situation_repository.lock(connection)
        row = situation_repository.lock_situations(connection, [situation_id]).get(situation_id)
        if row is None or row.status not in (SituationStatus.CONFIRMED, SituationStatus.PROPOSED):
            raise InvalidSituation("Lage nicht gefunden oder nicht mehr in Kraft.")
        if event_id not in situation_repository.events(connection, [event_id]):
            raise InvalidSituation("Ereignis nicht gefunden.")
        situation_repository.set_member(connection, situation_id, event_id, MemberStatus.CONFIRMED, MemberOrigin.ANALYST)
    return row.name


def parse_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise InvalidSituation("Name: bitte angeben.")
    if len(name) > MAX_NAME_CHARS:
        raise InvalidSituation(f"Name: höchstens {MAX_NAME_CHARS} Zeichen.")
    return name


def describe(engine: Engine, situation_id: int, raw_name: str | None, raw_description: str | None) -> bool:
    """Name and description as the analyst writes them; False when the Lage no longer exists."""
    name = parse_name(raw_name)
    description = (raw_description or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise InvalidSituation(f"Beschreibung: höchstens {MAX_DESCRIPTION_CHARS} Zeichen.")
    with engine.begin() as connection:
        return situation_repository.describe(connection, situation_id, name, description or None)


# What the pages show.

@dataclass(frozen=True)
class SituationRef:
    id: int
    name: str


@dataclass(frozen=True)
class SituationItem:
    id: int
    name: str
    description: str | None
    status: str
    created_at: datetime
    decided_at: datetime | None
    merged_into_id: int | None
    event_count: int  # in effect
    automatic_count: int
    proposed_count: int
    last_event_at: datetime | None
    dormant: bool

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class MemberView:
    event_id: int
    title: str
    status: str
    origin: str
    score: float | None
    added_at: datetime
    decided_at: datetime | None
    moved_to_id: int | None
    first_reported_at: datetime
    last_reported_at: datetime
    event: EventListItem | None = None  # its state and coverage now; None for a merged event
    changes: list = field(default_factory=list)  # dashboard_service.Change: its changes of state, the earliest first

    @property
    def status_label(self) -> str:
        return MEMBER_LABELS[self.status]

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS[self.origin]


@dataclass(frozen=True)
class SourceCount:
    name: str
    events: int


@dataclass(frozen=True)
class Proposed:
    situation: SituationItem
    members: list[MemberView]


@dataclass(frozen=True)
class SituationOverview:
    proposals: list[Proposed]
    situations: list[SituationItem]  # confirmed, active ones first
    rejected: int
    dismissed: int


@dataclass(frozen=True)
class SituationDetail:
    situation: SituationItem
    members: list[MemberView]  # in effect, the earliest event first: the timeline
    proposed: list[MemberView]  # waiting for the analyst: of a proposal, or resumptions of a dormant Lage
    removed: list[MemberView]
    sources: list[SourceCount]
    merge_choices: list[SituationRef]
    merged_into: SituationRef | None = None


def choices(engine: Engine) -> list[SituationRef]:
    with engine.connect() as connection:
        return [SituationRef(row.id, row.name) for row in situation_repository.choices(connection)]


def overview(engine: Engine) -> SituationOverview:
    with engine.connect() as connection:
        since = _open_since(connection)
        rows = situation_repository.listing(connection, list(SituationStatus))
        proposed_ids = [row.id for row in rows if row.status == SituationStatus.PROPOSED]
        member_rows = situation_repository.member_rows(connection, proposed_ids) if proposed_ids else []
    items = [_item(row, since) for row in rows]
    by_situation: dict[int, list[MemberView]] = defaultdict(list)
    for row in member_rows:
        if row.status in LIVE:
            by_situation[row.situation_id].append(_member(row))
    proposals = [Proposed(item, by_situation[item.id]) for item in items if item.status == SituationStatus.PROPOSED]
    confirmed = sorted((item for item in items if item.status == SituationStatus.CONFIRMED), key=lambda item: item.dormant)
    count = Counter(item.status for item in items)
    return SituationOverview(proposals, confirmed, count[SituationStatus.REJECTED], count[SituationStatus.DISMISSED])


def detail(engine: Engine, situation_id: int) -> SituationDetail | None:
    with engine.connect() as connection:
        since = _open_since(connection)
        row = next(iter(situation_repository.listing(connection, list(SituationStatus), situation_id)), None)
        if row is None:
            return None
        member_rows = situation_repository.member_rows(connection, [situation_id])
        live_ids = [each.event_id for each in member_rows if each.status in LIVE]
        versions = event_repository.versions_of(connection, live_ids) if live_ids else []
        sources = situation_repository.sources_of(connection, [each.event_id for each in member_rows if each.status in IN_EFFECT])
        merge_choices = [SituationRef(each.id, each.name) for each in situation_repository.choices(connection) if each.id != situation_id]
        merged_into = situation_repository.get(connection, row.merged_into_id) if row.merged_into_id else None
    listed = {item.id: item for item in event_service.listed(engine, live_ids)} if live_ids else {}
    changes = dashboard_service.changes_since(versions, datetime.min.replace(tzinfo=UTC))
    members = [
        _member(each, listed.get(each.event_id), changes.get(each.event_id, [])) for each in member_rows
    ]
    return SituationDetail(
        _item(row, since),
        [each for each in members if each.status in IN_EFFECT],
        [each for each in members if each.status == MemberStatus.PROPOSED],
        [each for each in members if each.status == MemberStatus.REMOVED],
        [SourceCount(each.name, each.events) for each in sources],
        merge_choices if row.status == SituationStatus.CONFIRMED else [],
        SituationRef(merged_into.id, merged_into.name) if merged_into else None,
    )


@dataclass(frozen=True)
class EventSituation:
    situation: SituationRef
    situation_status: str
    status: str  # of the belonging

    @property
    def status_label(self) -> str:
        if self.situation_status == SituationStatus.PROPOSED:
            return "vorgeschlagene Lage"
        return MEMBER_LABELS[self.status]


@dataclass(frozen=True)
class EventSituations:
    """The Lagen of an event, and those it can be added to."""

    situations: list[EventSituation]
    choices: list[SituationRef]


def of_event(engine: Engine, event_id: int) -> EventSituations:
    with engine.connect() as connection:
        rows = situation_repository.of_event(connection, event_id)
        offered = situation_repository.choices(connection)
    own = {row.id for row in rows}
    return EventSituations(
        [EventSituation(SituationRef(row.id, row.name), row.situation_status, row.status) for row in rows],
        [SituationRef(row.id, row.name) for row in offered if row.id not in own],
    )


def _open_since(connection: Connection) -> datetime:
    settings = platform_settings_service.load(connection)
    return datetime.now(UTC) - timedelta(days=settings.situation_open_days)


def _item(row: Row, since: datetime) -> SituationItem:
    return SituationItem(
        row.id, row.name, row.description, row.status, row.created_at, row.decided_at, row.merged_into_id, row.event_count,
        row.automatic_count, row.proposed_count, row.last_event_at,
        row.status == SituationStatus.CONFIRMED and row.last_event_at is not None and row.last_event_at < since,
    )


def _member(row: Row, event: EventListItem | None = None, changes: list | None = None) -> MemberView:
    return MemberView(
        row.event_id, row.title, row.status, row.origin, row.score, row.added_at, row.decided_at, row.moved_to_id,
        row.first_reported_at, row.last_reported_at, event, changes or [],
    )
