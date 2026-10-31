"""Event identity (userstories.md, Ereignisidentität und zeitliche Entwicklung, story 1): every event candidate joins
an existing event (Ereignis) or founds a new one, whose id is the stable event ID (datamodel.md, rule 1).

The same event is one incident with its direct continuation - an attack and its death toll, a quake and its
aftershocks - not the same topic. A candidate is compared with the events whose reports reach to within OPEN_FOR of
its own: by the cosine of the TF-IDF weights of the words of title, summary, place and actors, which the model writes
in English whatever the article's language. An event scores what its most similar report does. From ASSIGN_FROM the
candidate joins the best one, below NEW_BELOW it founds a new event; in between the LLM decides among the up to
CHOICES best. Never an event with a candidate of the same article: the model told those apart on purpose - unless that
one is of the article's reading a reprocessing replaced (reprocessing_service): its successor finds its event through
it, before it leaves.

The worker queues the candidates without an event once a minute, the earliest report first, so the first report
founds the event and the later ones find it. The job compares and asks outside any transaction and assigns in the one
that completes it, under an advisory lock, after comparing again: a decision the events changed under is made anew.

The development of an event (story 2): each report is weighed against the state in effect at its own time
(state_effect), and one that changes it adds a version (Ereigniszustand) in the same transaction. Versions are only
ever added. A report that contradicts the state changes nothing and stays beside it, as every report keeps its own
statements: nothing is merged into one fact (datamodel.md, rule 6).

Merges and splits by the analyst (story 3), and the reports a reprocessing replaced leaving their events, live in
event_correction_service; the event page shows them here.
"""
import json
import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Connection, Engine, Row

from app.db.models import AssignmentMethod, CorrectionKind, EventStatus
from app.jobs.errors import JobDeferred
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import event_correction_repository, event_repository
from app.services import job_service
from app.services.extraction_service import EVENT_TYPE_LABELS, STATUS_LABELS

logger = logging.getLogger(__name__)

OPEN_FOR = timedelta(days=7)
# Measured on 126 candidates of the dev data: from 0.45 on every pair was one incident, below 0.2 none was; in
# between both (an attack and the shelling of the same town the next day at 0.27, two banks' quarterly results at 0.41).
ASSIGN_FROM = 0.5
NEW_BELOW = 0.2
CHOICES = 3
# Queued per sweep: an assignment without the LLM takes milliseconds, with it a few seconds.
QUEUE_LIMIT = 200
LIST_LIMIT = 100
_WORD = re.compile(r"[^\W_]+")
# Words that say nothing about which incident a report is of, in the English the model writes.
STOPWORDS = frozenset(
    "a an the of in on at to for and or with from by as is are was were be been has have had its it this that these "
    "those after before over under into amid near against between during than not no new says said".split()
)

CHOICE_PROMPT = """You decide whether a news report is about one of several known events, for an event database. Answer
only with JSON in the given schema.

- The same event is one real-world incident with its direct continuation: an attack and its death toll, the arrests
  and funerals after it; an earthquake and its aftershocks and the rescue work; a strike and its end; a rate decision
  and the reactions to it.
- Not the same event: another incident of the same kind, topic or conflict, even in the same place or with the same
  actors - a second attack, the next rate decision, another company's quarterly results, another court case.
- reason: one short sentence on what decides it. same_as: the number of the known event the report is about, or null
  when it is about none of them."""
# Its reason before its answer: a model that names what decides it first decides better. Not stored.
MAX_REASON_CHARS = 300


class StateEffect(StrEnum):
    """What a report does to the state of its event (story 2)."""

    VERSION = "version"  # it changes the state: a new version
    CONFIRMS = "confirms"
    CONTRADICTS = "contradicts"  # kept beside the state, never merged into it
    NONE = "none"  # its status is unknown


EFFECT_LABELS = {
    StateEffect.VERSION: "Zustandswechsel",
    StateEffect.CONFIRMS: "bestätigt",
    StateEffect.CONTRADICTS: "widerspricht",
    StateEffect.NONE: "–",
}


def state_effect(current: str | None, reported: str) -> StateEffect:
    """The admin's rule of 2026-10-29: the earliest known status founds the state; while it is ongoing an update
    confirms it, an escalation or a closure changes it, a start or a resumption contradicts it; once closed only a
    resumption changes it, a closure confirms it, anything else contradicts it. The same status always confirms -
    another outlet reporting the same start or escalation. The migration 5d0c7e1a9b42 replayed the reports assigned
    before with a copy of this rule: change both together only if it is to apply to the past as well."""
    if reported == EventStatus.UNKNOWN:
        return StateEffect.NONE
    if current is None:
        return StateEffect.VERSION
    if reported == current:
        return StateEffect.CONFIRMS
    if current == EventStatus.CLOSURE:
        return StateEffect.VERSION if reported == EventStatus.RESUMPTION else StateEffect.CONTRADICTS
    if reported == EventStatus.UPDATE:
        return StateEffect.CONFIRMS
    if reported in (EventStatus.ESCALATION, EventStatus.CLOSURE):
        return StateEffect.VERSION
    return StateEffect.CONTRADICTS


class UnusableChoice(Exception):
    """The LLM's answer named no known event and not none either: retried with the job."""


@dataclass(frozen=True)
class Match:
    event_id: int
    score: float


@dataclass(frozen=True)
class Decision:
    method: AssignmentMethod
    event_id: int | None = None  # None: a new event
    score: float | None = None


def enqueue_due(engine: Engine) -> list[int]:
    """Queue an assignment for the candidates without an event, the earliest report first. The worker calls it only
    while Ollama serves the model, as an assignment may need it."""
    kind = job_service.ASSIGN_EVENT_JOB_KIND
    with engine.begin() as connection:
        due = event_repository.due(connection, kind, QUEUE_LIMIT)
        job_ids = [
            job_service.enqueue(connection, kind, {"candidate_id": candidate_id}, dedupe_key=f"{kind}:{candidate_id}")
            for candidate_id in due
        ]
    return [job_id for job_id in job_ids if job_id is not None]


def assign(engine: Engine, candidate_id: int, client: OllamaClient, limiter: LlmCallLimiter) -> Callable[[Connection], None] | None:
    """First phase of an assignment job: compare, and in between ask the LLM. Returns the effect that assigns, or None
    when the candidate is gone or has its event already. LlmError (app/llm/ollama.py) or UnusableChoice when the LLM
    brought no usable answer: retried with the job."""
    with engine.connect() as connection:
        candidate = event_repository.get_for_assignment(connection, candidate_id)
        if candidate is None or candidate.event_id is not None or candidate.replaced:
            return None
        found = matches(candidate, _reports(connection, candidate))
        decision = settled(found)
        asked = choices(found) if decision is None else []
        events = event_repository.events_for_choice(connection, [match.event_id for match in asked])
    if decision is None:
        by_id = {event.id: event for event in events}
        with limiter.slot():
            answer = client.chat_json(choice_messages(candidate, [by_id[match.event_id] for match in asked]), choice_schema(len(asked)))
        chosen = chosen_number(answer.content if answer.complete else "", len(asked))
        decision = Decision(AssignmentMethod.NEW) if chosen is None else Decision(AssignmentMethod.LLM, asked[chosen - 1].event_id)

    def store(connection: Connection) -> None:
        event_repository.lock_assignment(connection)
        current = event_repository.get_for_assignment(connection, candidate_id)
        if current is None or current.event_id is not None or current.replaced:
            return
        now = matches(current, _reports(connection, current))
        final, offered = settled(now), 0
        if final is None:
            if {match.event_id for match in choices(now)} != {match.event_id for match in asked}:
                # Another assignment changed the events to choose from since the LLM was asked: ask again.
                raise JobDeferred(datetime.now(UTC))
            score = next((match.score for match in now if match.event_id == decision.event_id), None)
            final, offered = Decision(decision.method, decision.event_id, score), len(asked)
        event_id = _apply(connection, current, final)
        # IDs, method and score only (datamodel.md, rule 13); `offered`: the events the LLM chose among, 0 when the
        # scores decided alone - a new event after an offer is one the LLM judged none of them.
        fields = {"candidate_id": candidate_id, "event_id": event_id, "method": final.method, "score": final.score}
        logger.info("Event assigned", extra=fields | {"offered": offered})

    return store


def _reports(connection: Connection, candidate: Row) -> list[Row]:
    return list(event_repository.reports_near(connection, candidate.moment, OPEN_FOR))


def _apply(connection: Connection, candidate: Row, decision: Decision) -> int:
    event_id = decision.event_id
    if event_id is None:
        # The founding report names the event; later reports leave its name as it is.
        values = {
            "title": candidate.title, "event_type": candidate.event_type, "country": candidate.country,
            "first_reported_at": candidate.moment, "last_reported_at": candidate.moment, "founding_candidate_id": candidate.id,
        }
        event_id = event_repository.create_event(connection, values)
    score = None if decision.score is None else round(decision.score, 3)
    event_repository.assign(connection, candidate.id, event_id, decision.method, score, candidate.moment)
    current = event_repository.state_at(connection, event_id, candidate.moment)
    if state_effect(current, candidate.status) == StateEffect.VERSION:
        event_repository.add_state(connection, event_id, candidate.status, candidate.moment, candidate.id)
    return event_id


def words(report) -> Counter:
    """The words of a report's title, summary, place and actors in lower case, without stop words and the shortest."""
    text = " ".join([report.title, report.summary, report.place or ""] + [actor["name"] for actor in report.actors])
    return Counter(word for word in _WORD.findall(text.lower()) if len(word) > 2 and word not in STOPWORDS)


def _weighting(documents: list[Counter]) -> Callable[[Counter], dict[str, float]]:
    """The TF-IDF weights of a document among `documents`: a word counts the more, the fewer of them have it."""
    frequency = Counter(word for document in documents for word in document)
    count = len(documents)

    def weights(document: Counter) -> dict[str, float]:
        # +1: a word all of a few documents have still counts a little.
        return {word: (1 + math.log(n)) * math.log((count + 1) / frequency[word]) for word, n in document.items()}

    return weights


def matches(candidate, reports: Iterable) -> list[Match]:
    """The events of the reports by their similarity to the candidate, the most similar first: TF-IDF cosine, the
    words weighted by how few of the reports and the candidate have them. An event with a report of the candidate's
    article is left out, unless that report's reading was replaced (`replaced`): it is the one the candidate replaces."""
    reports = list(reports)
    documents = [words(report) for report in reports]
    own = words(candidate)
    weights = _weighting([*documents, own])

    vector = weights(own)
    norm = math.sqrt(sum(value * value for value in vector.values()))
    excluded = {report.event_id for report in reports if report.article_id == candidate.article_id and not report.replaced}
    best: dict[int, float] = {}
    for report, document in zip(reports, documents, strict=True):
        if report.event_id in excluded:
            continue
        other = weights(document)
        other_norm = math.sqrt(sum(value * value for value in other.values()))
        score = sum(value * other.get(word, 0.0) for word, value in vector.items()) / (norm * other_norm) if norm and other_norm else 0.0
        best[report.event_id] = max(score, best.get(report.event_id, 0.0))
    return sorted((Match(event_id, score) for event_id, score in best.items()), key=lambda match: (-match.score, match.event_id))


def related(own: list, others: list) -> list[Match]:
    """The events of the reports `others` by their similarity to an event's reports `own`, the most similar first, as
    matches weighs a candidate: an event scores its report most similar to any of `own`. One sharing no word is left
    out. Only shared words count, so each report of `others` meets only the reports of `own` it shares one with."""
    documents = [words(report) for report in [*own, *others]]
    weights = _weighting(documents)
    vectors = [_unit(weights(document)) for document in documents]
    by_word: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for position, vector in enumerate(vectors[:len(own)]):
        for word, value in vector.items():
            by_word[word].append((position, value))
    best: dict[int, float] = {}
    for report, vector in zip(others, vectors[len(own):], strict=True):
        scores: Counter = Counter()
        for word, value in vector.items():
            for position, own_value in by_word.get(word, ()):
                scores[position] += value * own_value
        if scores:
            best[report.event_id] = max(max(scores.values()), best.get(report.event_id, 0.0))
    return sorted((Match(event_id, score) for event_id, score in best.items()), key=lambda match: (-match.score, match.event_id))


def _unit(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    return {word: value / norm for word, value in vector.items()} if norm else {}


def settled(found: list[Match]) -> Decision | None:
    """What the scores decide alone: the best event from ASSIGN_FROM, a new event when none reaches NEW_BELOW. None in
    between, where the LLM decides among choices(found)."""
    if found and found[0].score >= ASSIGN_FROM:
        return Decision(AssignmentMethod.SCORE, found[0].event_id, found[0].score)
    if not choices(found):
        return Decision(AssignmentMethod.NEW)
    return None


def choices(found: list[Match]) -> list[Match]:
    return [match for match in found[:CHOICES] if match.score >= NEW_BELOW]


def choice_messages(candidate, events: list) -> list[dict]:
    """The report and the known events for the LLM: an event by its name, its time and its latest report."""
    place = ", ".join(part for part in (candidate.place, candidate.country) if part) or "unknown"
    report = (
        f"Report, published {candidate.moment.date().isoformat()}:\nTitle: {candidate.title}\nSummary: {candidate.summary}\n"
        f"Place: {place}\nTime: {candidate.time_note or 'unknown'}"
    )
    known = "\n".join(
        f"{number}. {event.title} (reported {event.first_reported_at.date().isoformat()} to "
        f"{event.last_reported_at.date().isoformat()})\n   Latest report: {event.latest_summary}"
        for number, event in enumerate(events, start=1)
    )
    return [{"role": "system", "content": CHOICE_PROMPT}, {"role": "user", "content": f"{report}\n\nKnown events:\n{known}"}]


def choice_schema(count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
            "same_as": {"enum": [*range(1, count + 1), None]},
        },
        "required": ["reason", "same_as"],
        "additionalProperties": False,
    }


def chosen_number(content: str, count: int) -> int | None:
    """The number of the known event the LLM chose, None for none of them."""
    try:
        same_as = json.loads(content)["same_as"]
    except (ValueError, TypeError, KeyError):
        raise UnusableChoice("Antwort ohne same_as") from None
    if same_as is not None and (type(same_as) is not int or not 1 <= same_as <= count):
        raise UnusableChoice(f"same_as ausserhalb von 1 bis {count}")
    return same_as


METHOD_LABELS = {AssignmentMethod.NEW: "Neues Ereignis", AssignmentMethod.SCORE: "Ähnlichkeit", AssignmentMethod.LLM: "LLM"}
CORRECTION_LABELS = {CorrectionKind.MERGE: "Zusammenführung", CorrectionKind.SPLIT: "Aufteilung", CorrectionKind.REPROCESS: "Neuverarbeitung"}


def score_label(score: float | None) -> str:
    return f"{score:.2f}".replace(".", ",") if score is not None else ""


@dataclass(frozen=True)
class EventListItem:
    id: int
    title: str
    event_type: str
    country: str | None
    first_reported_at: datetime
    last_reported_at: datetime
    state: str | None  # of its latest version in effect; None while no report stated a known one
    report_count: int
    source_count: int

    @property
    def type_label(self) -> str:
        return EVENT_TYPE_LABELS[self.event_type]

    @property
    def state_label(self) -> str:
        return STATUS_LABELS[self.state] if self.state else "–"


@dataclass(frozen=True)
class EventOverview:
    events: list[EventListItem]  # the LIST_LIMIT most recently reported
    total: int
    waiting: int  # candidates without an event yet


@dataclass(frozen=True)
class ReportView:
    """A report of an event: a candidate with its article, and how it came to the event."""

    id: int
    title: str
    status: str
    assignment_method: str
    assignment_score: float | None
    article_id: int
    article_title: str | None
    article_url: str
    moment: datetime
    source_display_name: str
    effect: StateEffect = StateEffect.NONE  # on the state in effect at its time, as the versions stand now
    contradicted_state: str | None = None  # the state it contradicts
    moved_by: str | None = None  # the kind of the correction that brought it to the event, the latest
    founding: bool = False  # the event is named after it: never split off

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def effect_label(self) -> str:
        if self.effect == StateEffect.CONTRADICTS:
            return f"{EFFECT_LABELS[self.effect]} ({STATUS_LABELS[self.contradicted_state]})"
        return EFFECT_LABELS[self.effect]

    @property
    def method_label(self) -> str:
        """How it came to this event: by a correction, else as the assignment placed it."""
        return CORRECTION_LABELS[self.moved_by] if self.moved_by else self.assignment_label

    @property
    def assignment_label(self) -> str:
        """By what it was assigned: founded the event, its words, or the LLM - with the similarity it had."""
        label = METHOD_LABELS[self.assignment_method]
        if self.assignment_method == AssignmentMethod.LLM and self.assignment_score is not None:
            return f"{label} (Ähnlichkeit {score_label(self.assignment_score)})"
        if self.assignment_method == AssignmentMethod.SCORE:
            return f"{label} {score_label(self.assignment_score)}"
        return label


@dataclass(frozen=True)
class StateView:
    """A version of the event's state with the report that stated it."""

    id: int
    status: str
    valid_from: datetime
    recorded_at: datetime
    candidate_id: int | None
    report_title: str | None  # None once its article is gone
    time_note: str | None
    article_id: int | None
    source_display_name: str | None
    added_by_id: int | None = None  # the correction that appended it; None: its report's assignment
    withdrawn_by_id: int | None = None  # the correction it no longer follows from

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def withdrawn(self) -> bool:
        return self.withdrawn_by_id is not None



@dataclass(frozen=True)
class EventRef:
    id: int
    title: str


@dataclass(frozen=True)
class CorrectionView:
    """A correction as an event it concerns sees it: reports that came to it, or reports that left it."""

    id: int
    kind: str
    created_at: datetime
    reason: str | None
    incoming: bool  # the reports came to this event
    others: list[EventRef]  # the events they came from, or the one they went to
    candidate_ids: list[int]  # the reports that came or left

    @property
    def kind_label(self) -> str:
        return CORRECTION_LABELS[self.kind]

    @property
    def relation_label(self) -> str:
        if self.kind == CorrectionKind.MERGE:
            return "Aufgenommen:" if self.incoming else "Aufgegangen in"
        if self.kind == CorrectionKind.REPROCESS:
            return "Ersetzt durch die neue Extraktion ihres Artikels"
        return "Abgespalten aus" if self.incoming else "Abgespalten als"


@dataclass(frozen=True)
class EventDetail:
    event: EventListItem
    reports: list[ReportView] = field(default_factory=list)  # the earliest first
    states: list[StateView] = field(default_factory=list)  # the earliest first, the withdrawn ones too
    corrections: list[CorrectionView] = field(default_factory=list)  # the earliest first
    merged_into: EventRef | None = None

    @property
    def contradictions(self) -> int:
        return sum(report.effect == StateEffect.CONTRADICTS for report in self.reports)

    def correction(self, correction_id: int | None) -> CorrectionView | None:
        return next((correction for correction in self.corrections if correction.id == correction_id), None)


def overview(engine: Engine) -> EventOverview:
    with engine.connect() as connection:
        rows = event_repository.latest(connection, LIST_LIMIT)
        total = event_repository.count(connection)
        waiting = event_repository.count_unassigned(connection)
    return EventOverview(events=[_list_item(row) for row in rows], total=total, waiting=waiting)


def listed(engine: Engine, event_ids: Iterable[int]) -> list[EventListItem]:
    """These events as the list shows them, merged ones left out."""
    with engine.connect() as connection:
        return [_list_item(row) for row in event_repository.listed(connection, event_ids)]


def detail(engine: Engine, event_id: int) -> EventDetail | None:
    with engine.connect() as connection:
        row = event_repository.get(connection, event_id)
        if row is None:
            return None
        states = [_state(each) for each in event_repository.states(connection, event_id)]
        reports = [_report(each, states) for each in event_repository.reports(connection, event_id)]
        corrections = _corrections(connection, event_id)
        merged_into = None
        if row.merged_into_id is not None:
            merged_into = EventRef(row.merged_into_id, event_repository.titles(connection, [row.merged_into_id])[row.merged_into_id])
    # The latest correction that brought a report here tells how it came; the assignment's own record stays.
    moved_by = {
        candidate_id: correction.kind
        for correction in corrections if correction.incoming for candidate_id in correction.candidate_ids
    }
    reports = [
        replace(report, moved_by=moved_by.get(report.id), founding=report.id == row.founding_candidate_id) for report in reports
    ]
    counts = {"report_count": len(reports), "source_count": len({report.source_display_name for report in reports})}
    return EventDetail(_list_item(row, **counts), reports, states, corrections, merged_into)


def _corrections(connection: Connection, event_id: int) -> list[CorrectionView]:
    rows = event_correction_repository.of_event(connection, event_id)
    titles = event_repository.titles(connection, {row.event_id for row in rows} | {move["event_id"] for row in rows for move in row.moves})
    views = []
    for row in rows:
        incoming = row.event_id == event_id
        moves = row.moves if incoming else [move for move in row.moves if move["event_id"] == event_id]
        # A reprocessing's reports left the event for none.
        refs = [] if row.kind == CorrectionKind.REPROCESS else [move["event_id"] for move in moves] if incoming else [row.event_id]
        views.append(CorrectionView(
            id=row.id, kind=row.kind, created_at=row.created_at, reason=row.reason, incoming=incoming,
            others=[EventRef(ref, titles.get(ref, "")) for ref in refs],
            candidate_ids=[candidate_id for move in moves for candidate_id in move["candidate_ids"]],
        ))
    return views


def _list_item(row: Row, **values) -> EventListItem:
    mapping = dict(row._mapping) | values
    return EventListItem(**{each.name: mapping[each.name] for each in fields(EventListItem)})


def _report(row: Row, states: list[StateView]) -> ReportView:
    """A report with what it does to the version in effect at its time; the versions are the earliest first. A
    withdrawn version is in effect no more."""
    values = {each.name: row._mapping[each.name] for each in fields(ReportView) if each.name in row._mapping}
    report = ReportView(**values)
    states = [state for state in states if not state.withdrawn]
    if any(state.candidate_id == report.id for state in states):
        return replace(report, effect=StateEffect.VERSION)
    in_effect = [state.status for state in states if state.valid_from <= report.moment]
    current = in_effect[-1] if in_effect else None
    effect = state_effect(current, report.status)
    # A change by today's versions that made none when it was assigned: a late report has since added a version
    # before it. Neither confirming nor contradicting, it stays without a label rather than a guess.
    if effect == StateEffect.VERSION:
        effect = StateEffect.NONE
    return replace(report, effect=effect, contradicted_state=current if effect == StateEffect.CONTRADICTS else None)


def _state(row: Row) -> StateView:
    return StateView(**{each.name: row._mapping[each.name] for each in fields(StateView) if each.name in row._mapping})
