"""Wirkungsannahmen (userstories.md, Markt- und Wirkungsmodell, story 1; the admin's decisions of 2026-11-04): what
an event may do to companies, sectors and commodities - directly, or through a relationship of an entity it affects -
with direction, horizon, mechanism, the Belege for and against it and counterarguments, classified as belegt,
plausibel or spekulativ.

- The LLM proposes them, one job "Wirkungen bewerten" per event whose reports name an economic effect and an entity it
  may be about, behind live work and within a budget of its own (Plattform-Einstellung). It is shown the reports'
  effects and statements as numbered Belege with their quotes, the targets (the entities the reports name and those
  related to them by a relationship in effect) and those relationships, and chooses only from them.
- The classification is a rule in code (`classify`): belegt only when a cited Beleg is a report's effect on this very
  target in this direction, with its quote; plausibel through a relationship or a cited Beleg; spekulativ otherwise.
  The LLM's proposal is only ever capped, never raised.
- Versions are appended, never changed (datamodel.md, rule 3). A new assessment changes only what is still proposed:
  a change is a new version, one no longer proposed is superseded. What the analyst confirmed or rejected stays.
- An event is assessed again after a new report, at most once a day.
"""
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine

from app.db.models import (
    BACKFILL_PRIORITY,
    IMPACT_DIRECTIONS,
    IMPACT_HORIZONS,
    EntityKind,
    Expected,
    ImpactClass,
    ImpactOrigin,
    ImpactPath,
    ImpactProbability,
    ImpactStatus,
    RelationshipKind,
)
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import entity_repository, impact_repository, relationship_repository, reprocessing_repository
from app.services import event_study_service, instrument_service, job_service, platform_settings_service, relationship_service
from app.services.bulk import BulkResult
from app.services.entity_service import KIND_LABELS as ENTITY_KIND_LABELS
from app.services.extraction_service import DIRECTION_LABELS, HORIZON_LABELS, TARGET_KIND_LABELS
from app.services.extraction_service import KIND_LABELS as STATEMENT_KIND_LABELS

logger = logging.getLogger(__name__)

KIND = job_service.ASSESS_IMPACTS_JOB_KIND
# A change of prompt, schema or meaning is a new version: every proposed version names the assessment it came from.
# 2 (2026-11-04): mechanism and counterarguments without the lists' numbers - the model wrote "relationship 2".
# 3 (2026-11-04): probability apart from direction, abstention, and whether the event was expected (story 2).
PROMPT_VERSION = 3
BUDGET_PERIOD = timedelta(hours=1)
AGAIN_AFTER = timedelta(days=1)
TARGET_KINDS = (EntityKind.COMPANY, EntityKind.SUBSIDIARY, EntityKind.SECTOR, EntityKind.COMMODITY)
MAX_REPORTS = 10
MAX_ITEMS = 40
MAX_TARGETS = 30
MAX_RELATIONSHIPS = 20
MAX_HYPOTHESES = 8
MAX_CITED = 10
MAX_TEXT_CHARS = 600
MAX_NOTE_CHARS = 1000
MAX_REASON_CHARS = 300

STATUS_LABELS = {
    ImpactStatus.PROPOSED: "vorgeschlagen",
    ImpactStatus.CONFIRMED: "bestätigt",
    ImpactStatus.REJECTED: "verworfen",
    ImpactStatus.SUPERSEDED: "überholt",
}
CLASS_LABELS = {ImpactClass.EVIDENCED: "belegt", ImpactClass.PLAUSIBLE: "plausibel", ImpactClass.SPECULATIVE: "spekulativ"}
# Strongest first: a cap takes the later of two.
_CLASS_ORDER = (ImpactClass.EVIDENCED, ImpactClass.PLAUSIBLE, ImpactClass.SPECULATIVE)
ORIGIN_LABELS = {ImpactOrigin.LLM: "LLM", ImpactOrigin.ANALYST: "Analyst"}
PROBABILITY_LABELS = {ImpactProbability.HIGH: "hoch", ImpactProbability.MEDIUM: "mittel", ImpactProbability.LOW: "niedrig"}
# Highest first: a cap takes the later of two, a step down the next.
_PROBABILITY_ORDER = (ImpactProbability.HIGH, ImpactProbability.MEDIUM, ImpactProbability.LOW)
EXPECTED_LABELS = {Expected.YES: "erwartet", Expected.NO: "nicht erwartet", Expected.UNKNOWN: "unbekannt, ob erwartet"}
# The form's choice for no probability.
ABSTAIN = "abstain"
# The Vorlauf counts as a move the assumed way from this t value of the market model on (event_study_service).
PRICED_IN_T = 2.0
TOO_LITTLE_DATA = "Zu wenig Daten: kein Beleg und keine Beziehung."
# How the LLM reads a relationship, its subject first.
_RELATION_WORDS = {
    RelationshipKind.SUPPLIES: "supplies",
    RelationshipKind.COMPETES: "competes with",
    RelationshipKind.REGULATES: "regulates",
    RelationshipKind.DEPENDS_ON: "depends on",
}

PROMPT = f"""You assess the economic effects of a news event for a research database: which companies, sectors and
commodities it may affect, how, and how well that is founded. Use only what you are given; never invent a fact.

Each hypothesis:
- target: the number of one listed target.
- path: direct when the event itself affects the target; indirect when it affects the target through one of the
  listed relationships of an entity the event affects - then via is that relationship's number, else via is null.
- direction for the target: positive, negative, mixed or unclear. horizon: days, weeks, months, years or unknown.
- mechanism: how the event leads to the effect, in one or two sentences.
- counterarguments: what speaks against it, could cancel it out, or another outcome; never empty - if the evidence
  names none, say what would have to hold for the effect to occur.
- support: the numbers of the evidence that backs it; against: the numbers of the evidence that speaks against it.
- classification: evidenced only when an evidence item states this very effect on this target; plausible when it
  follows through a relationship, a source's forecast or a stated effect; speculative otherwise.
- probability: how likely the effect occurs at all - high, medium or low -, apart from its direction; null to
  abstain when the evidence does not allow a judgement, then abstention says why (else abstention is empty).
- expected: yes when the evidence says the event was announced, scheduled or widely expected before it happened
  (then the market may have priced it in), no when it came as a surprise, unknown otherwise; expected_support: the
  numbers of the evidence that says so.

At most {MAX_HYPOTHESES} hypotheses, the most relevant first; none when the event has no economic effect on the
listed targets. Write in English, mechanism and counterarguments as plain sentences that name the entities - never
the numbers of the lists, which the reader does not see."""


class UnusableAnswer(Exception):
    """The answer is not the schema's: retried with the job."""


class InvalidHypothesis(ValueError):
    """Invalid entries from the analyst's form. The message is shown as is."""


# What the LLM is shown


@dataclass(frozen=True)
class Item:
    """A Beleg the LLM may cite: a report's effect, or one of its statements."""

    number: int
    ref: dict  # {"statement_id": 1} | {"candidate_id": 2, "impact": 0}
    kind: str  # "impact" or "statement"
    text: str
    quote: str | None
    candidate_id: int
    target_entity_id: int | None = None  # of an effect: the entity the recognition linked its target to
    direction: str | None = None  # of an effect
    statement_kind: str | None = None

    @property
    def key(self) -> str:
        return ref_key(self.ref)


def ref_key(ref: Mapping) -> str:
    """A Beleg's form value: "s12" for a statement, "i5.0" for a report's first effect."""
    return f"s{ref['statement_id']}" if "statement_id" in ref else f"i{ref['candidate_id']}.{ref['impact']}"


def parse_ref(key: str) -> dict | None:
    try:
        if key.startswith("s"):
            return {"statement_id": int(key[1:])}
        if key.startswith("i"):
            candidate, position = key[1:].split(".")
            return {"candidate_id": int(candidate), "impact": int(position)}
    except ValueError:
        return None
    return None


@dataclass(frozen=True)
class Target:
    number: int
    entity_id: int
    name: str
    kind: str
    named: bool  # the event's reports name it: it may be affected directly


@dataclass(frozen=True)
class Context:
    event_id: int
    title: str
    reports: int
    summaries: list[str]
    items: list[Item]
    targets: list[Target]
    relationships: list[relationship_service.InEffect]
    named_ids: frozenset[int]

    def target(self, entity_id: int) -> Target | None:
        return next((each for each in self.targets if each.entity_id == entity_id), None)

    def relationship(self, relationship_id: int):
        return next((each for each in self.relationships if each.id == relationship_id), None)

    def items_of(self, refs: Iterable[Mapping]) -> list[Item]:
        by_key = {item.key: item for item in self.items}
        return [by_key[ref_key(ref)] for ref in refs if ref_key(ref) in by_key]


def context(connection: Connection, event_id: int) -> Context | None:
    """What an assessment of the event is shown; None for an event merged or gone."""
    event = impact_repository.event(connection, event_id)
    if event is None or event.merged_into_id is not None:
        return None
    reports = impact_repository.reports(connection, event_id, MAX_REPORTS)
    candidate_ids = [row.id for row in reports]
    linked = impact_repository.impact_targets(connection, candidate_ids) if candidate_ids else {}
    effects, said = [], []
    for row in reports:
        for position, impact in enumerate(row.impacts or []):
            text = (
                f"{impact['target']} ({impact['target_kind']}): {impact['direction']}, {impact['horizon']}"
                + (f" - {impact['description']}" if impact.get("description") else "")
            )
            effects.append(dict(
                ref={"candidate_id": row.id, "impact": position}, kind="impact", text=text, quote=impact.get("quote"),
                candidate_id=row.id, target_entity_id=linked.get((row.id, position)), direction=impact["direction"],
            ))
    for row in impact_repository.statements(connection, candidate_ids) if candidate_ids else []:
        said.append(dict(
            ref={"statement_id": row.id}, kind="statement", text=row.text, quote=row.quote, candidate_id=row.candidate_id,
            statement_kind=row.kind,
        ))
    # The effects first: they are what a hypothesis is evidenced by.
    items = [Item(number, **values) for number, values in enumerate((effects + said)[:MAX_ITEMS], start=1)]

    named = [row for row in entity_repository.of_event(connection, event_id)]
    named_ids = frozenset(row.id for row in named)
    targets = [(row.id, row.name, row.kind, True) for row in named if row.kind in TARGET_KINDS]
    relationships = []
    for relation in relationship_service.in_effect_around(connection, named_ids):
        sides = [(relation.subject_id, relation.subject_name, relation.subject_kind), (relation.object_id, relation.object_name, relation.object_kind)]
        # Usable when one side is named by the event and the other may be a target.
        if any(each[0] in named_ids for each in sides) and any(each[2] in TARGET_KINDS for each in sides):
            relationships.append(relation)
            for entity_id, name, kind in sides:
                if kind in TARGET_KINDS and entity_id not in named_ids and all(entity_id != each[0] for each in targets):
                    targets.append((entity_id, name, kind, False))
        if len(relationships) >= MAX_RELATIONSHIPS:
            break
    numbered = [Target(number, *values) for number, values in enumerate(targets[:MAX_TARGETS], start=1)]
    summaries = [f"{row.title}: {row.summary}" for row in reports]
    return Context(event_id, event.title, event.reports, summaries, items, numbered, relationships, named_ids)


def _relation_line(relation) -> str:
    return f"{relation.subject_name} {_RELATION_WORDS.get(relation.kind, relation.kind)} {relation.object_name}"


def messages(context: Context) -> list[dict]:
    lines = [f"Event: {context.title}", "", "Reports (latest first):"]
    lines += [f"- {summary}" for summary in context.summaries]
    lines += ["", "Evidence:"]
    for item in context.items:
        label = "effect named" if item.kind == "impact" else item.statement_kind
        lines.append(f"{item.number}. [{label}] {item.text}" + (f' - "{item.quote}"' if item.quote else ""))
    lines += ["", "Targets:"]
    lines += [f"{target.number}. {target.name} ({target.kind}){'' if target.named else ' - not named by the event'}" for target in context.targets]
    lines += ["", "Relationships:"]
    lines += [f"{number}. {_relation_line(relation)}" for number, relation in enumerate(context.relationships, start=1)] or ["none"]
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def _numbers_schema(count: int) -> dict:
    if not count:
        return {"type": "array", "items": {"type": "integer"}, "maxItems": 0}
    return {"type": "array", "items": {"enum": list(range(1, count + 1))}, "maxItems": MAX_CITED}


def schema(context: Context) -> dict:
    hypothesis = {
        "type": "object",
        "properties": {
            "target": {"enum": list(range(1, len(context.targets) + 1))},
            "path": {"enum": [each.value for each in ImpactPath]},
            "via": {"enum": [*range(1, len(context.relationships) + 1), None]},
            "direction": {"enum": list(IMPACT_DIRECTIONS)},
            "horizon": {"enum": list(IMPACT_HORIZONS)},
            "mechanism": {"type": "string", "maxLength": MAX_TEXT_CHARS},
            "counterarguments": {"type": "string", "maxLength": MAX_TEXT_CHARS},
            "support": _numbers_schema(len(context.items)),
            "against": _numbers_schema(len(context.items)),
            "classification": {"enum": [each.value for each in ImpactClass]},
            "probability": {"enum": [*(each.value for each in ImpactProbability), None]},
            "abstention": {"type": "string", "maxLength": MAX_REASON_CHARS},
            "expected": {"enum": [each.value for each in Expected]},
            "expected_support": _numbers_schema(len(context.items)),
        },
        "required": [
            "target", "path", "via", "direction", "horizon", "mechanism", "counterarguments", "support", "against",
            "classification", "probability", "abstention", "expected", "expected_support",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
            "hypotheses": {"type": "array", "items": hypothesis, "maxItems": MAX_HYPOTHESES},
        },
        "required": ["reason", "hypotheses"],
        "additionalProperties": False,
    }


# The rule


def classify(path: str, direction: str, target_entity_id: int, support: Sequence[Item], through_relationship: bool) -> ImpactClass:
    """The most a hypothesis may claim: belegt when a cited Beleg is a report's effect on this very target in this
    direction - the report states it, with its quote; plausibel through a relationship in effect or with any cited
    Beleg; spekulativ otherwise."""
    if path == ImpactPath.DIRECT and any(
        item.kind == "impact" and item.target_entity_id == target_entity_id and item.direction == direction and item.quote
        for item in support
    ):
        return ImpactClass.EVIDENCED
    if (path == ImpactPath.INDIRECT and through_relationship) or support:
        return ImpactClass.PLAUSIBLE
    return ImpactClass.SPECULATIVE


def capped(proposed: str | None, allowed: ImpactClass) -> ImpactClass:
    """The weaker of what was proposed and what the rule allows."""
    if proposed not in ImpactClass._value2member_map_:
        return allowed
    return max(ImpactClass(proposed), allowed, key=_CLASS_ORDER.index)


def cap_probability(
    proposed: str | None, abstention: str | None, classification: str, support: int, against: int,
    through_relationship: bool, priced_in: bool | None, expected: str | None,
) -> tuple[str | None, str | None]:
    """The probability a hypothesis may claim and, without one, why (requirements.md, Markt- und Wirkungsmodell,
    criterion 4): none without any Beleg and relationship - too little data; spekulativ, or more Belege against than
    for, at most low; priced in by the Vorlauf or expected by the reports one step lower. Only ever lowered."""
    if not support and not through_relationship:
        return None, TOO_LITTLE_DATA
    if proposed not in ImpactProbability._value2member_map_:
        return None, abstention or "Enthaltung ohne Begründung."
    level = ImpactProbability(proposed)
    if classification == ImpactClass.SPECULATIVE or against > support:
        level = max(level, ImpactProbability.LOW, key=_PROBABILITY_ORDER.index)
    if priced_in or expected == Expected.YES:
        level = _PROBABILITY_ORDER[min(_PROBABILITY_ORDER.index(level) + 1, len(_PROBABILITY_ORDER) - 1)]
    return level.value, None


def is_priced_in(direction: str, move) -> bool | None:
    """Whether the target's Vorlauf moved markedly the assumed way: None when unmeasured, or for a direction that is
    neither positive nor negative."""
    if move is None or move.t is None or move.abnormal is None or direction not in ("positive", "negative"):
        return None
    return abs(move.t) >= PRICED_IN_T and (move.abnormal > 0) == (direction == "positive")


@dataclass(frozen=True)
class Market:
    """What the Vorlauf of the target's Wertpapier says, for a version."""

    instrument_id: int | None = None
    abnormal: float | None = None
    t: float | None = None
    priced_in: bool | None = None

    def fields(self) -> dict:
        return {"pre_instrument_id": self.instrument_id, "pre_abnormal": self.abnormal, "pre_t": self.t, "priced_in": self.priced_in}


def market_of(direction: str, move) -> Market:
    if move is None:
        return Market()
    return Market(move.instrument_id, _rounded(move.abnormal), _rounded(move.t), is_priced_in(direction, move))


def _rounded(value: float | None) -> float | None:
    # Stored as compared: a later computation of the same prices gives the same version.
    return None if value is None else round(value, 6)


@dataclass(frozen=True)
class Proposal:
    target_entity_id: int
    path: str
    via_relationship_id: int | None
    via_entity_id: int | None
    direction: str
    horizon: str
    mechanism: str
    counterarguments: str
    support: list[dict]
    against: list[dict]
    proposed_classification: str | None
    classification: str
    proposed_probability: str | None = None
    llm_abstention: str | None = None  # the LLM's reason, before the rule
    expected: str | None = None
    expected_support: list[dict] = field(default_factory=list)
    market: Market = field(default_factory=Market)

    @property
    def key(self) -> tuple:
        return (self.target_entity_id, self.path, self.via_relationship_id)

    @property
    def settled(self) -> tuple[str | None, str | None]:
        return cap_probability(
            self.proposed_probability, self.llm_abstention, self.classification, len(self.support), len(self.against),
            self.via_relationship_id is not None, self.market.priced_in, self.expected,
        )

    def fields(self) -> dict:
        probability, abstention = self.settled
        return {
            "direction": self.direction, "horizon": self.horizon, "mechanism": self.mechanism,
            "counterarguments": self.counterarguments, "support": self.support, "against": self.against,
            "classification": self.classification, "probability": probability, "abstention": abstention,
            "expected": self.expected, "expected_support": self.expected_support, **self.market.fields(),
        }


def _text(value, limit: int = MAX_TEXT_CHARS) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _cited(numbers, context: Context) -> list[dict]:
    if not isinstance(numbers, list):
        return []
    found = []
    for number in numbers:
        if type(number) is int and 1 <= number <= len(context.items):
            ref = context.items[number - 1].ref
            if ref not in found:
                found.append(ref)
    return found[:MAX_CITED]


def _way(context: Context, target: Target, path: str, relationship_id: int | None) -> tuple[int | None, int | None] | None:
    """The relationship and the event's entity a hypothesis reaches its target through - (None, None) for a direct
    one -, or None when the way does not hold: a direct one needs a target the event names, an indirect one a
    relationship from an entity the event names to the target."""
    if path == ImpactPath.DIRECT:
        return (None, None) if target.named else None
    relation = context.relationship(relationship_id) if relationship_id is not None else None
    if relation is None or target.entity_id not in (relation.subject_id, relation.object_id):
        return None
    via = relation.other(target.entity_id)
    return (relation.id, via) if via in context.named_ids and via != target.entity_id else None


def proposals(content: str, context: Context) -> tuple[list[Proposal], int]:
    """The answer's hypotheses that hold, each once by target and way, and how many were left out."""
    try:
        answer = json.loads(content)
        items = answer["hypotheses"]
    except (ValueError, TypeError, KeyError):
        raise UnusableAnswer("Antwort ohne hypotheses") from None
    if not isinstance(items, list):
        raise UnusableAnswer("hypotheses ist keine Liste")
    found: dict[tuple, Proposal] = {}
    dropped = 0
    for item in items:
        proposal = _proposal(item, context) if isinstance(item, dict) else None
        if proposal is None or proposal.key in found:
            dropped += 1
            continue
        found[proposal.key] = proposal
    return list(found.values()), dropped


def _proposal(item: dict, context: Context) -> Proposal | None:
    number, path, via = item.get("target"), item.get("path"), item.get("via")
    if type(number) is not int or not 1 <= number <= len(context.targets) or path not in ImpactPath._value2member_map_:
        return None
    relationship_id = None
    if path == ImpactPath.INDIRECT:
        if type(via) is not int or not 1 <= via <= len(context.relationships):
            return None
        relationship_id = context.relationships[via - 1].id
    target = context.targets[number - 1]
    way = _way(context, target, path, relationship_id)
    direction, horizon, mechanism = item.get("direction"), item.get("horizon"), _text(item.get("mechanism"))
    if way is None or direction not in IMPACT_DIRECTIONS or horizon not in IMPACT_HORIZONS or not mechanism:
        return None
    support, against = _cited(item.get("support"), context), _cited(item.get("against"), context)
    proposed = item.get("classification") if item.get("classification") in ImpactClass._value2member_map_ else None
    allowed = classify(path, direction, target.entity_id, context.items_of(support), way[0] is not None)
    probability = item.get("probability") if item.get("probability") in ImpactProbability._value2member_map_ else None
    expected = item.get("expected") if item.get("expected") in Expected._value2member_map_ else Expected.UNKNOWN
    return Proposal(
        target.entity_id, path, way[0], way[1], direction, horizon, mechanism, _text(item.get("counterarguments")),
        support, against, proposed, capped(proposed, allowed), probability,
        _text(item.get("abstention"), MAX_REASON_CHARS) or None, expected, _cited(item.get("expected_support"), context),
    )


# The job


def enqueue_due(engine: Engine) -> list[int]:
    """Queue an assessment for each event due, within the hour's budget; none while it is 0."""
    with engine.begin() as connection:
        budget = platform_settings_service.load(connection).impact_llm_calls_per_hour
        if budget <= 0:
            return []
        left = budget - reprocessing_repository.jobs_since(connection, [KIND], BUDGET_PERIOD)
        if left <= 0:
            return []
        queued = []
        for event_id in impact_repository.due(connection, KIND, TARGET_KINDS, AGAIN_AFTER, left, PROMPT_VERSION):
            job_id = job_service.enqueue(connection, KIND, {"event_id": event_id}, dedupe_key=f"{KIND}:{event_id}", priority=BACKFILL_PRIORITY)
            if job_id:
                queued.append(job_id)
    return queued


def assess(engine: Engine, payload: Mapping, client: OllamaClient, limiter: LlmCallLimiter) -> Callable[[Connection], None] | None:
    """First phase of the job "Wirkungen bewerten": ask the LLM, check its answer. The effect stores the assessment
    and appends the versions that changed."""
    event_id = payload["event_id"]
    with engine.connect() as connection:
        shown = context(connection, event_id)
    if shown is None or not shown.targets:
        return None
    with limiter.slot():
        answer = client.chat_json(messages(shown), schema(shown))
    found, dropped = proposals(answer.content if answer.complete else "", shown)
    with engine.connect() as connection:
        moves = event_study_service.pre_moves(connection, event_id, {each.target_entity_id for each in found})
    found = [replace(each, market=market_of(each.direction, moves.get(each.target_entity_id))) for each in found]

    def store(connection: Connection) -> None:
        impact_repository.lock(connection, event_id)
        assessment_id = impact_repository.insert_assessment(connection, {
            "event_id": event_id, "model": answer.model, "prompt_version": PROMPT_VERSION, "reports": shown.reports,
            "hypotheses": len(found), "dropped": dropped,
        })
        changed = reconcile(connection, event_id, found, assessment_id)
        logger.info("Impacts assessed", extra={"event_id": event_id, "hypotheses": len(found), "dropped": dropped, "changed": changed})

    return store


def reconcile(connection: Connection, event_id: int, found: Sequence[Proposal], assessment_id: int) -> int:
    """Append what the assessment changes: a new hypothesis, a new version of one still proposed (or superseded) that
    reads differently, and a superseded version of one proposed it no longer proposes. The analyst's decisions stay.
    How many versions were appended."""
    existing = {(row.target_entity_id, row.path, row.via_relationship_id): row for row in impact_repository.hypotheses(connection, event_id)}
    appended = 0
    for proposal in found:
        row = existing.get(proposal.key)
        version = proposal.fields() | {
            "status": ImpactStatus.PROPOSED, "proposed_classification": proposal.proposed_classification,
            "proposed_probability": proposal.proposed_probability, "origin": ImpactOrigin.LLM, "assessment_id": assessment_id,
        }
        if row is None:
            hypothesis_id = impact_repository.insert_hypothesis(connection, {
                "event_id": event_id, "target_entity_id": proposal.target_entity_id, "path": proposal.path,
                "via_relationship_id": proposal.via_relationship_id, "via_entity_id": proposal.via_entity_id,
                "origin": ImpactOrigin.LLM,
            })
            impact_repository.insert_version(connection, hypothesis_id, version)
            appended += 1
        elif row.status in (ImpactStatus.CONFIRMED, ImpactStatus.REJECTED):
            continue
        elif row.status == ImpactStatus.SUPERSEDED or _fields(row) != proposal.fields():
            impact_repository.insert_version(connection, row.id, version)
            appended += 1
    keys = {proposal.key for proposal in found}
    for key, row in existing.items():
        if key not in keys and row.status == ImpactStatus.PROPOSED:
            impact_repository.insert_version(connection, row.id, _fields(row) | {
                "status": ImpactStatus.SUPERSEDED, "proposed_classification": row.proposed_classification,
                "proposed_probability": row.proposed_probability, "origin": ImpactOrigin.LLM, "assessment_id": assessment_id,
            })
            appended += 1
    return appended


def _fields(row) -> dict:
    return {
        "direction": row.direction, "horizon": row.horizon, "mechanism": row.mechanism, "counterarguments": row.counterarguments,
        "support": list(row.support), "against": list(row.against), "classification": row.classification,
        "probability": row.probability, "abstention": row.abstention, "expected": row.expected,
        "expected_support": list(row.expected_support), "pre_instrument_id": row.pre_instrument_id,
        "pre_abnormal": row.pre_abnormal, "pre_t": row.pre_t, "priced_in": row.priced_in,
    }


# The analyst


def confirm(engine: Engine, event_id: int, hypothesis_ids: Iterable[int], note: str = "") -> BulkResult:
    """Bestätigen: all or nothing; one already confirmed blocks."""
    return _review(engine, event_id, hypothesis_ids, ImpactStatus.CONFIRMED, _note(note))


def reject(engine: Engine, event_id: int, hypothesis_ids: Iterable[int], note: str) -> BulkResult:
    """Verwerfen, with a reason: all or nothing; one already rejected blocks."""
    return _review(engine, event_id, hypothesis_ids, ImpactStatus.REJECTED, _note(note, required=True))


def _review(engine: Engine, event_id: int, hypothesis_ids: Iterable[int], status: str, note: str | None) -> BulkResult:
    hypothesis_ids = sorted(set(hypothesis_ids))
    if not hypothesis_ids:
        return BulkResult(0, [])
    with engine.begin() as connection:
        impact_repository.lock(connection, event_id)
        rows = [row for row in impact_repository.hypotheses_by_id(connection, hypothesis_ids).values() if row.event_id == event_id]
        blocked = [_name(row) for row in rows if row.status == status]
        if blocked or not rows:
            return BulkResult(0, blocked)
        for row in rows:
            impact_repository.insert_version(connection, row.id, _fields(row) | {
                "status": status, "proposed_classification": None, "proposed_probability": None, "origin": ImpactOrigin.ANALYST,
                "note": note,
            })
    logger.info("Impact hypotheses reviewed", extra={"event_id": event_id, "status": status, "hypotheses": len(rows)})
    return BulkResult(len(rows), [])


def _name(row) -> str:
    return f"{row.target_name} ({DIRECTION_LABELS[row.direction]})"


def _note(text: str | None, required: bool = False) -> str | None:
    text = (text or "").strip()
    if required and not text:
        raise InvalidHypothesis("Bitte eine Begründung angeben.")
    if len(text) > MAX_NOTE_CHARS:
        raise InvalidHypothesis(f"Die Begründung ist länger als {MAX_NOTE_CHARS} Zeichen.")
    return text or None


@dataclass(frozen=True)
class Entries:
    """The analyst's entries, checked."""

    target_entity_id: int
    path: str
    via_relationship_id: int | None
    via_entity_id: int | None
    fields: dict
    note: str | None


def parse(
    form: Mapping[str, str], support: Sequence[str], against: Sequence[str], context: Context, keep_way: tuple | None = None,
    moves: Mapping[int, object] | None = None, expected_support: Sequence[Mapping] = (),
) -> Entries:
    """The form's entries against the event's targets, relationships and Belege; `keep_way` (target, path,
    relationship, via entity) for a change, which keeps them; `moves` the targets' Vorlauf (event_study_service).
    Classification and probability are the rules' at most."""
    errors = []
    if keep_way is None:
        target_id = int(form["target"]) if (form.get("target") or "").isdigit() else None
        target = context.target(target_id) if target_id is not None else None
        way_value = form.get("way") or ImpactPath.DIRECT
        path = ImpactPath.DIRECT if way_value == ImpactPath.DIRECT else ImpactPath.INDIRECT
        relationship_id = int(way_value) if path == ImpactPath.INDIRECT and way_value.isdigit() else None
        way = _way(context, target, path, relationship_id) if target is not None else None
        if target is None:
            errors.append("Bitte ein Ziel wählen.")
        elif way is None:
            errors.append(
                "Direkt nur auf eine Entität, die seine Berichte nennen." if path == ImpactPath.DIRECT
                else "Indirekt nur über eine Beziehung zwischen dem Ziel und einer Entität, die seine Berichte nennen."
            )
        keep_way = (target_id, path, *(way or (None, None)))
    direction, horizon = form.get("direction"), form.get("horizon")
    if direction not in IMPACT_DIRECTIONS:
        errors.append("Bitte eine Richtung wählen.")
    if horizon not in IMPACT_HORIZONS:
        errors.append("Bitte einen Horizont wählen.")
    mechanism, counterarguments = (form.get("mechanism") or "").strip(), (form.get("counterarguments") or "").strip()
    if not mechanism:
        errors.append("Bitte den Mechanismus beschreiben.")
    if not counterarguments:
        errors.append("Bitte Gegenargumente nennen - oder was gelten müsste, damit die Wirkung eintritt.")
    if max(len(mechanism), len(counterarguments)) > MAX_TEXT_CHARS:
        errors.append(f"Mechanismus und Gegenargumente je höchstens {MAX_TEXT_CHARS} Zeichen.")
    try:
        note = _note(form.get("note"))
    except InvalidHypothesis as error:
        errors.append(str(error))
        note = None
    probability, reason = form.get("probability"), _text(form.get("abstention"), MAX_REASON_CHARS)
    if probability != ABSTAIN and probability not in ImpactProbability._value2member_map_:
        errors.append("Bitte eine Wahrscheinlichkeit wählen oder sich enthalten.")
    elif probability == ABSTAIN and not reason:
        errors.append("Bitte begründen, warum keine Wahrscheinlichkeit.")
    expected = form.get("expected") if form.get("expected") in Expected._value2member_map_ else Expected.UNKNOWN
    if errors:
        raise InvalidHypothesis(" ".join(errors))
    cited = [ref for ref in (parse_ref(each) for each in support) if ref]
    countered = [ref for ref in (parse_ref(each) for each in against) if ref]
    known = {item.key for item in context.items}
    cited = [ref for ref in cited if ref_key(ref) in known]
    countered = [ref for ref in countered if ref_key(ref) in known]
    target_id, path, relationship_id, via_id = keep_way
    allowed = classify(path, direction, target_id, context.items_of(cited), relationship_id is not None)
    classification = capped(form.get("classification"), allowed)
    market = market_of(direction, (moves or {}).get(target_id))
    settled = cap_probability(
        None if probability == ABSTAIN else probability, reason or None, classification, len(cited), len(countered),
        relationship_id is not None, market.priced_in, expected,
    )
    fields = {
        "direction": direction, "horizon": horizon, "mechanism": mechanism, "counterarguments": counterarguments,
        "support": cited, "against": countered, "classification": classification, "probability": settled[0],
        "abstention": settled[1], "expected": expected, "expected_support": list(expected_support), **market.fields(),
    }
    return Entries(target_id, path, relationship_id, via_id, fields, note)


def create(engine: Engine, event_id: int, form: Mapping[str, str], support: Sequence[str], against: Sequence[str]) -> int:
    """Neue Wirkungsannahme by the analyst: confirmed from the start. One with the same target and way exists: change
    that one instead."""
    with engine.begin() as connection:
        shown = context(connection, event_id)
        if shown is None:
            raise InvalidHypothesis("Ereignis nicht gefunden oder zusammengeführt.")
        target = int(form["target"]) if (form.get("target") or "").isdigit() else None
        moves = event_study_service.pre_moves(connection, event_id, [target]) if target is not None else {}
        entries = parse(form, support, against, shown, moves=moves)
        impact_repository.lock(connection, event_id)
        existing = {(row.target_entity_id, row.path, row.via_relationship_id): row for row in impact_repository.hypotheses(connection, event_id)}
        if (entries.target_entity_id, entries.path, entries.via_relationship_id) in existing:
            raise InvalidHypothesis("Diese Wirkungsannahme gibt es schon: bitte dort ändern.")
        hypothesis_id = impact_repository.insert_hypothesis(connection, {
            "event_id": event_id, "target_entity_id": entries.target_entity_id, "path": entries.path,
            "via_relationship_id": entries.via_relationship_id, "via_entity_id": entries.via_entity_id, "origin": ImpactOrigin.ANALYST,
        })
        impact_repository.insert_version(connection, hypothesis_id, entries.fields | {
            "status": ImpactStatus.CONFIRMED, "origin": ImpactOrigin.ANALYST, "note": entries.note,
        })
    logger.info("Impact hypothesis created", extra={"event_id": event_id, "hypothesis_id": hypothesis_id})
    return hypothesis_id


def change(engine: Engine, hypothesis_id: int, form: Mapping[str, str], support: Sequence[str], against: Sequence[str]) -> bool:
    """Ändern: a new version by the analyst, confirmed; target and way stay. False for none such."""
    with engine.begin() as connection:
        row = impact_repository.hypotheses_by_id(connection, [hypothesis_id]).get(hypothesis_id)
        if row is None:
            return False
        shown = context(connection, row.event_id) or _empty_context(row.event_id)
        keep = (row.target_entity_id, row.path, row.via_relationship_id, row.via_entity_id)
        moves = event_study_service.pre_moves(connection, row.event_id, [row.target_entity_id])
        entries = parse(form, support, against, _with_cited(shown, connection, row), keep, moves, row.expected_support)
        impact_repository.lock(connection, row.event_id)
        impact_repository.insert_version(connection, hypothesis_id, entries.fields | {
            "status": ImpactStatus.CONFIRMED, "origin": ImpactOrigin.ANALYST, "note": entries.note,
        })
    logger.info("Impact hypothesis changed", extra={"event_id": row.event_id, "hypothesis_id": hypothesis_id})
    return True


def _empty_context(event_id: int) -> Context:
    return Context(event_id, "", 0, [], [], [], [], frozenset())


def _with_cited(shown: Context, connection: Connection, row) -> Context:
    """The context with the Belege the hypothesis cites already, should they be beyond what an assessment is shown."""
    known = {item.key for item in shown.items}
    extra = [each for each in _resolve(connection, [*row.support, *row.against]).values() if each.key not in known]
    items = shown.items + [Item(len(shown.items) + number, each.ref, each.kind, each.text, each.quote, each.candidate_id,
                                each.target_entity_id, each.direction, each.statement_kind)
                           for number, each in enumerate(extra, start=1)]
    return Context(shown.event_id, shown.title, shown.reports, shown.summaries, items, shown.targets, shown.relationships, shown.named_ids)


def _resolve(connection: Connection, refs: Iterable[Mapping]) -> dict[str, Item]:
    """The Belege these references point to, by key, wherever their reports went since; one gone is left out."""
    refs = list(refs)
    statement_ids = [ref["statement_id"] for ref in refs if "statement_id" in ref]
    candidate_ids = {ref["candidate_id"] for ref in refs if "candidate_id" in ref}
    found: dict[str, Item] = {}
    for row in impact_repository.statements(connection, statement_ids=statement_ids) if statement_ids else []:
        ref = {"statement_id": row.id}
        found[ref_key(ref)] = Item(0, ref, "statement", row.text, row.quote, row.candidate_id, statement_kind=row.kind)
    reports = impact_repository.reports_by_id(connection, candidate_ids) if candidate_ids else {}
    linked = impact_repository.impact_targets(connection, candidate_ids) if candidate_ids else {}
    for ref in refs:
        report = reports.get(ref.get("candidate_id"))
        if report is None or not 0 <= ref["impact"] < len(report.impacts or []):
            continue
        impact = report.impacts[ref["impact"]]
        text = f"{impact['target']} ({TARGET_KIND_LABELS.get(impact['target_kind'], impact['target_kind'])}): " \
               f"{DIRECTION_LABELS.get(impact['direction'], impact['direction'])}, {HORIZON_LABELS.get(impact['horizon'], impact['horizon'])}" \
               + (f" - {impact['description']}" if impact.get("description") else "")
        found[ref_key(ref)] = Item(0, dict(ref), "impact", text, impact.get("quote"), report.id,
                                   linked.get((report.id, ref["impact"])), impact["direction"])
    return found


# Views


@dataclass(frozen=True)
class BelegView:
    key: str
    label: str  # "Wirkung" or the statement's kind
    text: str
    quote: str | None
    report_id: int
    report_title: str | None
    article_url: str | None
    source_display_name: str | None


@dataclass(frozen=True)
class Likelihood:
    """How likely a version's effect is, apart from its direction, and what lowered it (story 2)."""

    probability: str | None = None
    proposed_probability: str | None = None
    abstention: str | None = None
    expected: str | None = None
    expected_support: list[BelegView] = field(default_factory=list)
    pre_symbol: str | None = None
    pre_abnormal: float | None = None
    pre_t: float | None = None
    priced_in: bool | None = None

    @property
    def assessed(self) -> bool:
        """False for a version from before story 2: neither a probability nor an abstention."""
        return self.probability is not None or self.abstention is not None

    @property
    def label(self) -> str:
        if self.probability:
            return PROBABILITY_LABELS[self.probability]
        return "Enthaltung" if self.abstention else "nicht bewertet"

    @property
    def lowered_from(self) -> str | None:
        """What the LLM proposed, when the rule took it lower or to an abstention."""
        if self.proposed_probability and self.proposed_probability != self.probability:
            return PROBABILITY_LABELS[self.proposed_probability]
        return None

    @property
    def expected_label(self) -> str | None:
        return EXPECTED_LABELS.get(self.expected) if self.expected else None

    @property
    def pre_label(self) -> str | None:
        """The Vorlauf of the target's Wertpapier, as the pages state it."""
        if self.pre_symbol is None:
            return None
        if self.pre_abnormal is None or self.pre_t is None:
            return f"Vorlauf {self.pre_symbol}: nicht messbar"
        abnormal = f"{self.pre_abnormal * 100:+.2f}".replace(".", ",")
        t = f"{self.pre_t:.2f}".replace(".", ",")
        return f"Vorlauf {self.pre_symbol}: {abnormal} % abnormal, t {t}"


def _likelihood(row, items: Mapping[str, Item], reports: Mapping[int, object]) -> Likelihood:
    return Likelihood(
        row.probability, row.proposed_probability, row.abstention, row.expected,
        [each for each in (_beleg(items, reports, ref) for ref in row.expected_support) if each],
        row.pre_symbol, row.pre_abnormal, row.pre_t, row.priced_in,
    )


@dataclass(frozen=True)
class HypothesisView:
    id: int
    event_id: int
    target_id: int
    target_name: str
    target_kind: str
    target_merged_into_id: int | None
    path: str
    via_name: str | None
    relationship: str | None  # "Shell plc liefert an Foo AG"
    number: int
    status: str
    direction: str
    horizon: str
    mechanism: str
    counterarguments: str
    classification: str
    proposed_classification: str | None
    origin: str
    changed_at: datetime
    note: str | None
    support: list[BelegView] = field(default_factory=list)
    against: list[BelegView] = field(default_factory=list)
    instruments: list = field(default_factory=list)  # instrument_service.InstrumentView of the target
    event_title: str | None = None
    likelihood: Likelihood = field(default_factory=Likelihood)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def classification_label(self) -> str:
        return CLASS_LABELS[self.classification]

    @property
    def capped_from(self) -> str | None:
        """What the LLM proposed, when the rule took it down."""
        if self.proposed_classification and self.proposed_classification != self.classification:
            return CLASS_LABELS[self.proposed_classification]
        return None

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS[self.direction]

    @property
    def horizon_label(self) -> str:
        return HORIZON_LABELS[self.horizon]

    @property
    def target_kind_label(self) -> str:
        return ENTITY_KIND_LABELS.get(self.target_kind, self.target_kind)

    @property
    def way_label(self) -> str:
        if self.path == ImpactPath.DIRECT:
            return "direkt"
        return f"indirekt über {self.via_name}" + (f" ({self.relationship})" if self.relationship else "")

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS[self.origin]

    @property
    def open(self) -> bool:
        return self.status in (ImpactStatus.PROPOSED, ImpactStatus.CONFIRMED)


@dataclass(frozen=True)
class AssessmentView:
    model: str
    prompt_version: int
    created_at: datetime
    hypotheses: int
    dropped: int


@dataclass(frozen=True)
class EventImpacts:
    hypotheses: list[HypothesisView]
    assessment: AssessmentView | None

    @property
    def current(self) -> list[HypothesisView]:
        return [each for each in self.hypotheses if each.open]

    @property
    def closed(self) -> list[HypothesisView]:
        return [each for each in self.hypotheses if not each.open]


@dataclass(frozen=True)
class VersionView:
    number: int
    status: str
    direction: str
    horizon: str
    mechanism: str
    counterarguments: str
    classification: str
    proposed_classification: str | None
    origin: str
    model: str | None
    prompt_version: int | None
    note: str | None
    created_at: datetime
    support: list[BelegView]
    against: list[BelegView]
    likelihood: Likelihood = field(default_factory=Likelihood)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def classification_label(self) -> str:
        return CLASS_LABELS[self.classification]

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS[self.direction]

    @property
    def horizon_label(self) -> str:
        return HORIZON_LABELS[self.horizon]

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS[self.origin] + (f" ({self.model}, Prompt {self.prompt_version})" if self.model else "")


@dataclass(frozen=True)
class TargetChoice:
    entity_id: int
    name: str
    kind_label: str
    named: bool


@dataclass(frozen=True)
class WayChoice:
    value: str  # "direct" or the relationship's id
    label: str


@dataclass(frozen=True)
class FormChoices:
    targets: list[TargetChoice]
    ways: list[WayChoice]
    belege: list[BelegView]


@dataclass(frozen=True)
class HypothesisDetail:
    hypothesis: HypothesisView
    versions: list[VersionView]
    choices: FormChoices


def _beleg(connection_items: Mapping[str, Item], reports: Mapping[int, object], ref: Mapping) -> BelegView | None:
    item = connection_items.get(ref_key(ref))
    if item is None:
        return None
    report = reports.get(item.candidate_id)
    label = "Wirkung" if item.kind == "impact" else STATEMENT_KIND_LABELS.get(item.statement_kind, item.statement_kind)
    return BelegView(
        item.key, label, item.text, item.quote, item.candidate_id, getattr(report, "title", None),
        getattr(report, "article_url", None), getattr(report, "source_display_name", None),
    )


def _views(connection: Connection, rows: Sequence) -> list[HypothesisView]:
    refs = [ref for row in rows for ref in (*row.support, *row.against, *row.expected_support)]
    items = _resolve(connection, refs)
    reports = impact_repository.reports_by_id(connection, {item.candidate_id for item in items.values()}) if items else {}
    relationships = {}
    for row in rows:
        if row.via_relationship_id is not None and row.via_relationship_id not in relationships:
            relationships[row.via_relationship_id] = _relationship_label(connection, row.via_relationship_id)
    views = []
    for row in rows:
        mapping = row._mapping
        views.append(HypothesisView(
            row.id, row.event_id, row.target_entity_id, row.target_name, row.target_kind, row.target_merged_into_id, row.path,
            row.via_name, relationships.get(row.via_relationship_id), row.number, row.status, row.direction, row.horizon,
            row.mechanism, row.counterarguments, row.classification, row.proposed_classification, row.version_origin,
            row.changed_at, row.note,
            [each for each in (_beleg(items, reports, ref) for ref in row.support) if each],
            [each for each in (_beleg(items, reports, ref) for ref in row.against) if each],
            [], mapping.get("event_title"), _likelihood(row, items, reports),
        ))
    return views


def _with_instruments(engine: Engine, views: list[HypothesisView]) -> list[HypothesisView]:
    """Each with the traded Wertpapiere of its target and their latest price."""
    by_entity: dict[int, list] = {}
    for instrument in instrument_service.of_entities(engine, {view.target_id for view in views}):
        by_entity.setdefault(instrument.entity_id, []).append(instrument)
    return [replace(view, instruments=by_entity.get(view.target_id, [])) for view in views]


def _relationship_label(connection: Connection, relationship_id: int) -> str | None:
    row = relationship_repository.get(connection, relationship_id)
    if row is None:
        return None
    return f"{row.subject_name} {relationship_service.KIND_TITLES.get(row.kind, row.kind)} {row.object_name}"


def of_event(engine: Engine, event_id: int) -> EventImpacts:
    with engine.connect() as connection:
        views = _views(connection, impact_repository.hypotheses(connection, event_id))
        latest = impact_repository.latest_assessment(connection, event_id)
    assessment = AssessmentView(latest.model, latest.prompt_version, latest.created_at, latest.hypotheses, latest.dropped) if latest else None
    return EventImpacts(_with_instruments(engine, views), assessment)


def about_entity(engine: Engine, entity_id: int, limit: int = 50) -> list[HypothesisView]:
    """The proposed and confirmed hypotheses about the entity, the latest changed first."""
    with engine.connect() as connection:
        rows = impact_repository.of_entity(connection, entity_id, (ImpactStatus.PROPOSED, ImpactStatus.CONFIRMED), limit)
        return _views(connection, rows)


def choices(connection: Connection, shown: Context) -> FormChoices:
    targets = [TargetChoice(each.entity_id, each.name, ENTITY_KIND_LABELS.get(each.kind, each.kind), each.named) for each in shown.targets]
    ways = [WayChoice(ImpactPath.DIRECT, "direkt")] + [
        WayChoice(str(each.id), f"indirekt: {each.subject_name} {relationship_service.KIND_TITLES.get(each.kind, each.kind)} {each.object_name}")
        for each in shown.relationships
    ]
    # Shown as on the hypothesis's page, in German - not as the LLM reads them.
    resolved = _resolve(connection, [item.ref for item in shown.items])
    reports = impact_repository.reports_by_id(connection, {item.candidate_id for item in shown.items}) if shown.items else {}
    belege = [each for each in (_beleg(resolved, reports, item.ref) for item in shown.items) if each]
    return FormChoices(targets, ways, belege)


def form_choices(engine: Engine, event_id: int) -> FormChoices | None:
    with engine.connect() as connection:
        shown = context(connection, event_id)
        return choices(connection, shown) if shown else None


def detail(engine: Engine, hypothesis_id: int) -> HypothesisDetail | None:
    with engine.connect() as connection:
        rows = impact_repository.hypotheses_by_id(connection, [hypothesis_id])
        if hypothesis_id not in rows:
            return None
        row = rows[hypothesis_id]
        [view] = _views(connection, [row])
        versions = impact_repository.versions(connection, hypothesis_id)
        items = _resolve(connection, [ref for version in versions for ref in (*version.support, *version.against, *version.expected_support)])
        reports = impact_repository.reports_by_id(connection, {item.candidate_id for item in items.values()}) if items else {}
        version_views = [
            VersionView(
                version.number, version.status, version.direction, version.horizon, version.mechanism, version.counterarguments,
                version.classification, version.proposed_classification, version.origin, version.model, version.prompt_version,
                version.note, version.created_at,
                [each for each in (_beleg(items, reports, ref) for ref in version.support) if each],
                [each for each in (_beleg(items, reports, ref) for ref in version.against) if each],
                _likelihood(version, items, reports),
            )
            for version in versions
        ]
        shown = context(connection, row.event_id) or _empty_context(row.event_id)
        form = choices(connection, _with_cited(shown, connection, row))
    return HypothesisDetail(_with_instruments(engine, [view])[0], version_views, form)
