"""Chancen (userstories.md, Dashboard, Recherche und Alerts, story 5; the admin's decisions of 2026-11-04): new and
developing events whose Wirkungsannahmen point at a Wertpapier that has not moved much yet, so the analyst finds what
the market may not have reacted to yet instead of what already moved. No trading advice: Seismo never trades.

- Events with a report within the period, not closed, not merged; their Wirkungsannahmen proposed or confirmed, with a
  probability, positive or negative, for a target with a US Wertpapier with daily prices.
- The move since the Ereignistag to the last close both prices reach, by the market model against the ETF of the
  sector the event names most, else SPY (event_study_service.moves_since). Not moved much: |t| under 1, within its
  usual noise; a move of |t| 1 or more against the assumed direction is a chance as well, named as such; one in its
  direction has moved already. No close since the Ereignistag yet: nothing moved. A Vorlauf that priced it in, a gap or
  too few days for the model: no chance - each counted, never shown as one.
- Direction, probability, classification, horizon and the move side by side, never one score; ordered as chosen.

Computed whenever a page asks, nothing stored.
"""
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from app.db.models import EventStatus
from app.repositories import event_repository
from app.services import event_study_service, impact_service
from app.services.event_service import STATUS_LABELS
from app.services.event_study_service import Status, number_label, percent_label

PERIODS = {"24h": ("24 Stunden", timedelta(hours=24)), "7d": ("7 Tage", timedelta(days=7)), "30d": ("30 Tage", timedelta(days=30))}
DEFAULT_PERIOD = "7d"
LIST_LIMIT = 200
# Within the usual noise: the market model's |t| under this.
QUIET_T = 1.0

PROBABILITY_ORDER = {"high": 0, "medium": 1, "low": 2}
CLASSIFICATION_ORDER = {"evidenced": 0, "plausible": 1, "speculative": 2}
HORIZON_ORDER = {"days": 0, "weeks": 1, "months": 2, "years": 3}
SORTS = {
    "recent": "Neuestes Ereignis",
    "probability": "Wahrscheinlichkeit",
    "classification": "Einstufung",
    "move": "Kleinste Bewegung",
    "horizon": "Horizont",
}
DIRECTION_CHOICES = {"positive": "positiv", "negative": "negativ"}
PROBABILITY_CHOICES = {"high": "hoch", "medium": "mittel", "low": "niedrig"}
CLASSIFICATION_CHOICES = {"evidenced": "belegt", "plausible": "plausibel", "speculative": "spekulativ"}
HORIZON_CHOICES = {"days": "Tage", "weeks": "Wochen", "months": "Monate", "years": "Jahre"}


class Kind:
    """What the move since the Ereignistag makes of a Wirkungsannahme."""

    NO_CLOSE = "no_close"  # a chance: nothing moved yet
    QUIET = "quiet"  # a chance: within its usual noise
    AGAINST = "against"  # a chance: moved against the assumed direction
    MOVED = "moved"  # moved in its direction already
    PRICED_IN = "priced_in"  # the Vorlauf priced it in
    UNMEASURED = "unmeasured"  # a gap, or too few days for the market model
    NO_PRICE = "no_price"  # no US Wertpapier with daily prices


CHANCES = (Kind.NO_CLOSE, Kind.QUIET, Kind.AGAINST)
KIND_LABELS = {
    Kind.NO_CLOSE: "noch kein Schluss seit dem Ereignis",
    Kind.QUIET: "kaum bewegt",
    Kind.AGAINST: "gegenläufig bewegt",
    Kind.MOVED: "schon in Richtung bewegt",
    Kind.PRICED_IN: "im Vorlauf eingepreist",
    Kind.UNMEASURED: "nicht messbar",
    Kind.NO_PRICE: "ohne US-Wertpapier mit Kursen",
}


def kind_of(direction: str, priced_in: bool | None, move) -> str:
    """The rule: what a Wirkungsannahme's move since the Ereignistag (event_study_service.Move, None without a
    Wertpapier) makes of it."""
    if move is None:
        return Kind.NO_PRICE
    if priced_in:
        return Kind.PRICED_IN
    if move.status is None:
        return Kind.NO_CLOSE
    if move.status != Status.DONE or move.t is None:
        return Kind.UNMEASURED
    if abs(move.t) < QUIET_T:
        return Kind.QUIET
    with_direction = move.t > 0 if direction == "positive" else move.t < 0
    return Kind.MOVED if with_direction else Kind.AGAINST


@dataclass(frozen=True)
class Criteria:
    direction: str = ""
    probability: str = ""
    classification: str = ""
    horizon: str = ""
    sort: str = "recent"

    def matches(self, hypothesis) -> bool:
        return (
            (not self.direction or hypothesis.direction == self.direction)
            and (not self.probability or hypothesis.likelihood.probability == self.probability)
            and (not self.classification or hypothesis.classification == self.classification)
            and (not self.horizon or hypothesis.horizon == self.horizon)
        )

    def query(self, **changes) -> str:
        values = {key: getattr(self, key) for key in ("direction", "probability", "classification", "horizon", "sort")} | changes
        return "&".join(f"{key}={value}" for key, value in values.items() if value and not (key == "sort" and value == "recent"))


def parse(params: Mapping[str, str]) -> Criteria:
    """The criteria from the URL; anything unknown is no criterion."""
    def choice(name, allowed):
        value = params.get(name, "")
        return value if value in allowed else ""

    return Criteria(
        choice("direction", DIRECTION_CHOICES), choice("probability", PROBABILITY_CHOICES),
        choice("classification", CLASSIFICATION_CHOICES), choice("horizon", HORIZON_CHOICES), choice("sort", SORTS) or "recent",
    )


@dataclass(frozen=True)
class Opportunity:
    event_id: int
    event_title: str
    event_state: str | None
    first_reported_at: datetime
    last_reported_at: datetime
    new: bool  # first reported within the period
    hypothesis: object  # impact_service.HypothesisView
    move: object  # event_study_service.Move
    kind: str

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def state_label(self) -> str:
        return STATUS_LABELS.get(self.event_state, "unbekannt") if self.event_state else "unbekannt"

    @property
    def move_label(self) -> str | None:
        if self.move.status is None:
            return None
        return f"{percent_label(self.move.abnormal)} abnormal, t {number_label(self.move.t)}, {self.move.days} Handelstage"

    @property
    def abs_t(self) -> float:
        return abs(self.move.t) if self.move.t is not None else 0.0


def order(items: list[Opportunity], sort: str) -> list[Opportunity]:
    def recent(each):
        return (-each.last_reported_at.timestamp(), each.event_id, each.hypothesis.id)

    keys = {
        "recent": recent,
        "probability": lambda each: (PROBABILITY_ORDER.get(each.hypothesis.likelihood.probability, 9), *recent(each)),
        "classification": lambda each: (CLASSIFICATION_ORDER.get(each.hypothesis.classification, 9), *recent(each)),
        "move": lambda each: (each.abs_t, *recent(each)),
        "horizon": lambda each: (HORIZON_ORDER.get(each.hypothesis.horizon, 9), *recent(each)),
    }
    return sorted(items, key=keys.get(sort, recent))


@dataclass(frozen=True)
class Overview:
    period: str
    since: datetime
    criteria: Criteria
    opportunities: list[Opportunity]
    total: int  # chances matching, before the limit
    counts: dict[str, int]  # per kind, of the matching hypotheses
    events: int  # events active within the period

    @property
    def periods(self) -> dict[str, str]:
        return {key: label for key, (label, _) in PERIODS.items()}

    @property
    def left_out(self) -> list[tuple[str, int]]:
        """What is not shown as a chance, and how many."""
        return [(KIND_LABELS[kind], self.counts[kind]) for kind in KIND_LABELS if kind not in CHANCES and self.counts.get(kind)]


def parse_period(raw: str | None) -> str:
    return raw if raw in PERIODS else DEFAULT_PERIOD


def opportunities(engine: Engine, since: datetime, criteria: Criteria = Criteria(), now: datetime | None = None,
                  limit: int = LIST_LIMIT) -> tuple[list[Opportunity], int, Counter, int]:
    """The chances of the events active since `since`, ordered as the criteria say; with their total, the count of
    every kind and the number of events."""
    now = now or datetime.now(UTC)
    with engine.connect() as connection:
        events = {
            row.id: row for row in event_repository.active_since(connection, since) if row.state != EventStatus.CLOSURE
        }
        hypotheses = [
            each for each in impact_service.open_of_events(connection, events)
            if each.likelihood.probability and each.direction in ("positive", "negative") and criteria.matches(each)
        ]
        targets = defaultdict(set)
        for each in hypotheses:
            targets[each.event_id].add(each.target_id)
        moves = event_study_service.moves_since(connection, targets, now)
    counts: Counter = Counter()
    found = []
    for each in hypotheses:
        move = moves.get((each.event_id, each.target_id))
        kind = kind_of(each.direction, each.likelihood.priced_in, move)
        counts[kind] += 1
        if kind in CHANCES:
            event = events[each.event_id]
            found.append(Opportunity(
                event.id, event.title, event.state, event.first_reported_at, event.last_reported_at, event.first_reported_at >= since,
                each, move, kind,
            ))
    ordered = order(found, criteria.sort)
    return ordered[:limit], len(ordered), counts, len(events)


def overview(engine: Engine, period: str = DEFAULT_PERIOD, criteria: Criteria = Criteria(), now: datetime | None = None) -> Overview:
    period = parse_period(period)
    now = now or datetime.now(UTC)
    since = now - PERIODS[period][1]
    found, total, counts, events = opportunities(engine, since, criteria, now)
    return Overview(period, since, criteria, found, total, dict(counts), events)
