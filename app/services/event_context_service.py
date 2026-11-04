"""Events in context (userstories.md, Dashboard, Recherche und Alerts, story 4): what an event's reports leave
uncertain or contradict, and the earlier events comparable with it - each shown as it is, never folded into a single
score (requirements.md, Dashboard, Recherche und Alerts, criterion 4; datamodel.md, rules 6 and 7).

Uncertainty is counted, not weighed: the statements by kind and by their two levels, the reports by the level of their
inference, the reports that contradict the state in effect at their time, the impacts on one target that reports see
in opposite directions, the reports the LLM assigned and the uncertain name forms.

Comparable earlier events are those whose last report came before this event's first: one incident is one event, so
an earlier one of the same kind is another incident, not a report this one missed (that is event_correction_service's
Ähnliche Ereignisse). Chosen in the query - of the same type, or naming the most of its entities -, then compared
without a dependency, as the Lagen compare events (situation_service): TF-IDF over the words of the reports and their
entities. Each is shown with why - type, country, the entities shared, the similarity - and how it went on: its state,
an escalation, its closure, its duration.
"""
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine

from app.db.models import AssignmentMethod, ConfidenceLevel, EventStatus, StatementKind
from app.llm.event_schema import Direction
from app.repositories import event_context_repository, event_repository, situation_repository
from app.services import event_service, situation_service
from app.services.entity_service import EntityRef
from app.services.event_service import EventListItem, StateEffect, score_label
from app.services.extraction_service import EVENT_TYPE_LABELS, STATUS_LABELS

# The candidates each way of choosing earlier events gives the comparison: enough for the similar ones among many of
# a type, few enough to compare while the page loads.
POOL = 200
# Shown on the event page.
EARLIER_LIMIT = 10
# Below it an earlier event shares hardly more than its type's common words (measured on the dev data, 2026-11-04).
MIN_SIMILARITY = 0.1

LEVEL_ORDER = (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW, None)
LEVEL_NAMES = {ConfidenceLevel.HIGH: "hoch", ConfidenceLevel.MEDIUM: "mittel", ConfidenceLevel.LOW: "niedrig", None: "unbekannt"}
KIND_PLURALS = {
    StatementKind.FACT: ("Tatsache", "Tatsachen"),
    StatementKind.CLAIM: ("Behauptung", "Behauptungen"),
    StatementKind.SOURCE_FORECAST: ("Quellenprognose", "Quellenprognosen"),
    StatementKind.INTERPRETATION: ("Interpretation", "Interpretationen"),
}
ONGOING = (EventStatus.START, EventStatus.UPDATE, EventStatus.ESCALATION, EventStatus.RESUMPTION)


@dataclass(frozen=True)
class Levels:
    """How many have each level of one kind (datamodel.md, rule 7): high, medium, low, unknown."""

    high: int = 0
    medium: int = 0
    low: int = 0
    unknown: int = 0

    @classmethod
    def of(cls, counted: Iterable[tuple[str | None, int]]) -> "Levels":
        totals: dict[str | None, int] = defaultdict(int)
        for level, count in counted:
            totals[level if level in LEVEL_NAMES else None] += count
        return cls(*(totals[level] for level in LEVEL_ORDER))

    @property
    def counts(self) -> tuple[int, int, int, int]:
        return self.high, self.medium, self.low, self.unknown

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def weak(self) -> int:
        """Low or unknown."""
        return self.low + self.unknown

    @property
    def label(self) -> str:
        parts = [f"{count} {LEVEL_NAMES[level]}" for level, count in zip(LEVEL_ORDER, self.counts, strict=True) if count]
        return " · ".join(parts) or "–"


@dataclass(frozen=True)
class ReportRef:
    id: int
    title: str


@dataclass(frozen=True)
class ImpactConflict:
    """One target that reports see affected in opposite directions: kept side by side (rule 6)."""

    target: str  # the entity's name, else the target as the first report writes it
    entity_id: int | None
    positive: list[ReportRef]
    negative: list[ReportRef]


@dataclass(frozen=True)
class UncertainForm:
    value: str
    entity_id: int
    entity_name: str


@dataclass(frozen=True)
class Uncertainty:
    """What an event's reports leave uncertain or contradict, each on its own."""

    reports: int = 0
    statements: int = 0
    kinds: dict[str, int] = field(default_factory=dict)  # by kind, in the order of KIND_PLURALS
    extraction: Levels = Levels()  # of the statements
    source_quality: Levels = Levels()  # of the statements' quotes
    inference: Levels = Levels()  # of the reports
    contradicting: list[ReportRef] = field(default_factory=list)  # the state in effect at their time
    impact_conflicts: list[ImpactConflict] = field(default_factory=list)
    llm_assigned: int = 0  # reports whose event the LLM chose, the words leaving it open
    uncertain_forms: list[UncertainForm] = field(default_factory=list)

    @property
    def kinds_label(self) -> str:
        parts = [f"{count} {KIND_PLURALS[kind][count != 1]}" for kind, count in self.kinds.items() if count]
        return ", ".join(parts) or "keine Aussagen"

    @property
    def contradictions(self) -> int:
        return len(self.contradicting) + len(self.impact_conflicts)


def levels_by_event(rows: Iterable) -> dict[int, tuple[dict[str, int], Levels, Levels]]:
    """Per event, from the statements counted by kind and levels: the kinds, the extraction and the source levels."""
    kinds: dict[int, dict[str, int]] = defaultdict(lambda: dict.fromkeys(KIND_PLURALS, 0))
    extraction: dict[int, list] = defaultdict(list)
    quality: dict[int, list] = defaultdict(list)
    for row in rows:
        kinds[row.event_id][row.kind] = kinds[row.event_id].get(row.kind, 0) + row.count
        extraction[row.event_id].append((row.extraction_confidence, row.count))
        quality[row.event_id].append((row.source_quality, row.count))
    return {event_id: (kinds[event_id], Levels.of(extraction[event_id]), Levels.of(quality[event_id])) for event_id in kinds}


def impact_conflicts(reports: Iterable, targets: dict[tuple[int, int], tuple[int, str]]) -> list[ImpactConflict]:
    """The targets one report sees positively and another negatively, by name. A target is its entity where the
    impact names one (`targets`: by report and the impact's place, the entity's id and name), else its name as written,
    in any case. Mixed and unclear contradict nothing, nor one report seeing both ways on its own - one effect in the
    short term, another later - (seen on the dev data, 2026-11-04): reports contradict each other."""
    seen: dict[tuple, dict] = {}
    for report in reports:
        for position, impact in enumerate(report.impacts or []):
            direction = impact.get("direction")
            if direction not in (Direction.POSITIVE, Direction.NEGATIVE):
                continue
            entity = targets.get((report.id, position))
            name = " ".join(str(impact.get("target") or "").split())
            if entity is None and not name:
                continue
            key = ("entity", entity[0]) if entity else ("name", name.casefold())
            found = seen.setdefault(key, {"target": entity[1] if entity else name, "entity_id": entity[0] if entity else None,
                                          Direction.POSITIVE: {}, Direction.NEGATIVE: {}})
            found[direction][report.id] = ReportRef(report.id, report.title)
    conflicts = [
        ImpactConflict(found["target"], found["entity_id"], list(found[Direction.POSITIVE].values()), list(found[Direction.NEGATIVE].values()))
        for found in seen.values() if _opposed(found[Direction.POSITIVE], found[Direction.NEGATIVE])
    ]
    return sorted(conflicts, key=lambda conflict: conflict.target.casefold())


def _opposed(positive: dict[int, ReportRef], negative: dict[int, ReportRef]) -> bool:
    """A report on one side and another one on the other."""
    return any(one != other for one in positive for other in negative)


def contradicting(reports: Iterable, versions: list) -> list[ReportRef]:
    """The reports that contradict the state in effect at their time (event_service.report_effect)."""
    return [
        ReportRef(report.id, report.title) for report in reports
        if event_service.report_effect(report.id, report.status, report.moment, versions)[0] == StateEffect.CONTRADICTS
    ]


def uncertainty_of(engine: Engine, event_ids: Iterable[int]) -> dict[int, Uncertainty]:
    """Every one of the events with what its reports leave uncertain or contradict; an event without reports with
    nothing."""
    event_ids = list(dict.fromkeys(event_ids))
    if not event_ids:
        return {}
    with engine.connect() as connection:
        levels = levels_by_event(event_context_repository.statement_levels(connection, event_ids))
        reports = event_context_repository.reports_of(connection, event_ids)
        targets = {(row.candidate_id, row.position): (row.entity_id, row.name)
                   for row in event_context_repository.impact_entities(connection, event_ids)}
        forms = event_context_repository.uncertain_forms(connection, event_ids)
        versions = event_repository.versions_of(connection, event_ids)
    by_event: dict[int, list] = defaultdict(list)
    for report in reports:
        by_event[report.event_id].append(report)
    versions_by_event: dict[int, list] = defaultdict(list)
    for version in versions:
        versions_by_event[version.event_id].append(version)
    forms_by_event: dict[int, list[UncertainForm]] = defaultdict(list)
    for form in forms:
        forms_by_event[form.event_id].append(UncertainForm(form.value, form.entity_id, form.entity_name))
    found = {}
    for event_id in event_ids:
        own = by_event[event_id]
        kinds, extraction, quality = levels.get(event_id, ({}, Levels(), Levels()))
        found[event_id] = Uncertainty(
            reports=len(own),
            statements=sum(kinds.values()),
            kinds=kinds,
            extraction=extraction,
            source_quality=quality,
            inference=Levels.of((report.inference_confidence, 1) for report in own),
            contradicting=contradicting(own, versions_by_event[event_id]),
            impact_conflicts=impact_conflicts(own, targets),
            llm_assigned=sum(report.assignment_method == AssignmentMethod.LLM for report in own),
            uncertain_forms=forms_by_event[event_id],
        )
    return found


# Comparable earlier events.

@dataclass(frozen=True)
class Outcome:
    """How an earlier event went on, as its versions stand now."""

    state: str | None
    escalated: bool  # a version not withdrawn is an escalation
    closed_at: datetime | None  # since when it is closed, if it is
    duration: timedelta  # from its first report to its last

    @property
    def label(self) -> str:
        parts = [STATUS_LABELS[self.state] if self.state else "Zustand unbekannt"]
        if self.escalated:
            parts.append("eskaliert")
        return ", ".join(parts)

    @property
    def duration_label(self) -> str:
        return duration_label(self.duration)


def outcome(event: EventListItem, versions: list) -> Outcome:
    """Its state now, whether it escalated, and when the closure in effect began: the first of the closures in a row
    at the end of its versions (not withdrawn, the earliest first) - a repeated closure changed nothing."""
    closed_at = None
    for version in versions:
        if version.status == EventStatus.CLOSURE:
            closed_at = closed_at or version.valid_from
        else:
            closed_at = None
    return Outcome(
        state=event.state,
        escalated=any(version.status == EventStatus.ESCALATION for version in versions),
        closed_at=closed_at if event.state == EventStatus.CLOSURE else None,
        duration=event.last_reported_at - event.first_reported_at,
    )


def duration_label(duration: timedelta) -> str:
    hours = duration.total_seconds() / 3600
    if hours < 1:
        return "unter einer Stunde"
    if hours < 48:
        return "1 Stunde" if int(hours) == 1 else f"{int(hours)} Stunden"
    return f"{int(hours // 24)} Tage"


@dataclass(frozen=True)
class EarlierEvent:
    event: EventListItem
    similarity: float  # of the words and entities of their reports: what orders them, shown beside the reasons
    shared_types: list[str]  # the types both state, as labels
    shared_countries: list[str]
    shared_entities: list[EntityRef]  # by name
    outcome: Outcome
    uncertainty: Uncertainty

    @property
    def similarity_label(self) -> str:
        return score_label(self.similarity)


@dataclass(frozen=True)
class Comparison:
    events: list[EarlierEvent] = field(default_factory=list)  # the most similar first

    def count(self, kind: str) -> int:
        """Of the earlier events: `closed`, `escalated` (closed or not), `ongoing` or `unknown`."""
        if kind == "escalated":
            return sum(each.outcome.escalated for each in self.events)
        states = {"closed": (EventStatus.CLOSURE,), "ongoing": ONGOING, "unknown": (None,)}[kind]
        return sum(each.outcome.state in states for each in self.events)


def earlier_events(engine: Engine, event_id: int) -> Comparison:
    """The earlier events most similar to this one, at most EARLIER_LIMIT, from MIN_SIMILARITY. None for a merged
    event, or one without reports."""
    with engine.connect() as connection:
        event = event_repository.get(connection, event_id)
        if event is None or event.merged_into_id is not None:
            return Comparison()
        facts = event_context_repository.facts_of(connection, [event_id])
        types = {row.event_type for row in facts}
        own_entities = {row.entity_id for row in situation_repository.entities_of(connection, [event_id])}
        before = event.first_reported_at
        pool = list(dict.fromkeys(
            event_context_repository.earlier_of_type(connection, event_id, before, types, POOL)
            + event_context_repository.earlier_sharing(connection, event_id, before, own_entities, POOL)
        ))
        if not pool:
            return Comparison()
        compared = [event_id, *pool]
        reports = situation_repository.reports_of(connection, compared)
        mentions = situation_repository.entities_of(connection, compared)
        vectors = situation_service.vectors(situation_service.documents(reports, mentions))
        own_vector = vectors.get(event_id, {})
        scores = {other: situation_service.similarity(own_vector, vectors.get(other, {})) for other in pool}
        chosen = sorted((other for other in pool if scores[other] >= MIN_SIMILARITY), key=lambda other: (-scores[other], other))
        chosen = chosen[:EARLIER_LIMIT]
        if not chosen:
            return Comparison()
        their_facts = event_context_repository.facts_of(connection, chosen)
        versions = event_repository.versions_of(connection, chosen)
    entities_by_event: dict[int, set[int]] = defaultdict(set)
    for mention in mentions:
        entities_by_event[mention.event_id].add(mention.entity_id)
    shared_entity_ids = {other: entities_by_event[other] & own_entities for other in chosen}
    with engine.connect() as connection:
        names = event_context_repository.entity_names(connection, set().union(*shared_entity_ids.values()))
    countries = {row.country for row in facts if row.country}
    facts_by_event: dict[int, list] = defaultdict(list)
    for row in their_facts:
        facts_by_event[row.event_id].append(row)
    versions_by_event: dict[int, list] = defaultdict(list)
    for version in versions:
        versions_by_event[version.event_id].append(version)
    listed = {item.id: item for item in event_service.listed(engine, chosen)}
    uncertainty = uncertainty_of(engine, chosen)
    found = []
    for other in chosen:
        item = listed.get(other)
        if item is None:  # merged meanwhile
            continue
        their_types = {row.event_type for row in facts_by_event[other]}
        their_countries = {row.country for row in facts_by_event[other] if row.country}
        found.append(EarlierEvent(
            event=item,
            similarity=scores[other],
            shared_types=[EVENT_TYPE_LABELS[each] for each in sorted(types & their_types)],
            shared_countries=sorted(countries & their_countries),
            shared_entities=sorted((EntityRef(entity_id, names[entity_id]) for entity_id in shared_entity_ids[other] if entity_id in names),
                                   key=lambda ref: ref.name.casefold()),
            outcome=outcome(item, versions_by_event[other]),
            uncertainty=uncertainty[other],
        ))
    return Comparison(found)
