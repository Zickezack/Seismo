"""The hit rate of the Prognosen broken down by what may explain it, and over time (userstories.md, Prognoseprotokoll
und Evaluation, story 3; the admin's decisions of 2026-11-05).

- Of the measured outcomes of one horizon - 20 trading days unless chosen otherwise: a Wirkungsannahme logs one per
  horizon, so pooling them would count it up to three times -, per model and prompt version, event type,
  classification, way, sector, probability (and horizon, when all are chosen), and per month of logging.
- Each group with its count, the events it comes from - forecasts of one event move together, so the band is rather
  too narrow -, its hits, the mean excess return the forecast's way, and a Wilson band of the hit rate.
- Judged against chance (50 %) only from 30 measured on, and as the backtests judge (backtest_service.threshold): the
  band is widened by Bonferroni over every group judged on the page, so it holds for all of them at once - many small
  groups otherwise show patterns that are noise. Computed when the page loads; nothing stored.
"""
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import Engine

from app.db.models import FORECAST_HORIZONS, ImpactPath
from app.repositories import forecast_repository
from app.services.backtest_service import MIN_TRADES, PROBABILITY_RANK, threshold
from app.services.event_study_service import percent_label, share_label
from app.services.extraction_service import EVENT_TYPE_LABELS
from app.services.forecast_service import PROBABILITY_LABELS
from app.services.impact_service import CLASS_LABELS

CHANCE = 0.5
# A group is judged from as many measured outcomes as a backtest's test period needs trades.
MIN_JUDGED = MIN_TRADES
DEFAULT_HORIZON = "20"
HORIZON_CHOICES = {**{str(each): each for each in FORECAST_HORIZONS}, "all": None}
HORIZON_LABELS = {**{str(each): f"{each} Handelstage" for each in FORECAST_HORIZONS}, "all": "Alle zusammen"}
WAY_LABELS = {ImpactPath.DIRECT: "direkt", ImpactPath.INDIRECT: "indirekt über eine Beziehung"}
MARKET_LABEL = "keine Branche (gegen SPY)"
MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember",
)
VERDICT_LABELS = {
    "above": "besser als Zufall",
    "below": "schlechter als Zufall",
    "even": "nicht unterscheidbar",
    "few": "zu wenige",
}


def wilson(hits: int, count: int, z: float) -> tuple[float, float] | None:
    """The Wilson score interval of a share at `z`; None without a count."""
    if count <= 0:
        return None
    share = hits / count
    denominator = 1 + z * z / count
    centre = (share + z * z / (2 * count)) / denominator
    half = z * math.sqrt(share * (1 - share) / count + z * z / (4 * count * count)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    count: int
    events: int
    hits: int
    mean_signed: float | None
    low: float | None
    high: float | None

    @property
    def judged(self) -> bool:
        return self.count >= MIN_JUDGED

    @property
    def verdict(self) -> str:
        if not self.judged or self.low is None or self.high is None:
            return "few"
        if self.low > CHANCE:
            return "above"
        if self.high < CHANCE:
            return "below"
        return "even"

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS[self.verdict]

    @property
    def rate_label(self) -> str:
        return share_label(self.hits / self.count) if self.count else "–"

    @property
    def band_label(self) -> str:
        if self.low is None or self.high is None:
            return "–"
        return f"{share_label(self.low)} bis {share_label(self.high)}"

    @property
    def mean_label(self) -> str:
        return percent_label(self.mean_signed)


@dataclass(frozen=True)
class Breakdown:
    key: str
    title: str
    groups: list[Group]


@dataclass(frozen=True)
class Evaluation:
    horizon: str  # a key of HORIZON_CHOICES
    total: Group
    breakdowns: list[Breakdown]
    months: list[Group]
    judged: int  # groups judged on the page, the total among them: Bonferroni's count
    z: float

    @property
    def min_judged(self) -> int:
        return MIN_JUDGED

    @property
    def z_label(self) -> str:
        return f"{self.z:.2f}".replace(".", ",")

    @property
    def level_label(self) -> str:
        """The confidence of each band alone that makes 95 % for all judged at once."""
        return f"{(1 - 0.05 / max(self.judged, 1)) * 100:.2f}".rstrip("0").rstrip(",.").replace(".", ",") + " %"


# How a row falls into a group of each breakdown: its key and label, and how the groups are ordered.
_Classify = Callable[[object], tuple[str, str]]


def _made_by(row) -> tuple[str, str]:
    if row.model is None:
        return "analyst", "Analyst"
    return f"llm:{row.model}:{row.prompt_version}", f"LLM ({row.model}, Prompt {row.prompt_version})"


def _sector(row) -> tuple[str, str]:
    if row.sector_key is None:
        return "market", MARKET_LABEL
    return f"sector:{row.sector_key}", row.sector_name or row.sector_key


def _month(row) -> tuple[str, str]:
    moment = row.created_at.astimezone(UTC)
    return f"{moment.year:04d}-{moment.month:02d}", f"{MONTH_NAMES[moment.month - 1]} {moment.year}"


def _by_label(group: Group):
    return group.label.casefold()


_BREAKDOWNS: list[tuple[str, str, _Classify, Callable[[Group], object]]] = [
    ("made_by", "Modell und Prompt", _made_by, _by_label),
    ("event_type", "Ereignistyp", lambda row: (row.event_type, EVENT_TYPE_LABELS.get(row.event_type, row.event_type)), _by_label),
    ("classification", "Klassifikation", lambda row: (row.classification, CLASS_LABELS.get(row.classification, row.classification)), _by_label),
    ("way", "Weg", lambda row: (row.path, WAY_LABELS.get(row.path, row.path)), _by_label),
    ("sector", "Branche", _sector, _by_label),
    ("horizon", "Horizont", lambda row: (str(row.horizon), f"{row.horizon} Handelstage"), lambda group: int(group.key)),
    (
        "probability", "Wahrscheinlichkeit", lambda row: (row.probability, PROBABILITY_LABELS.get(row.probability, row.probability)),
        lambda group: PROBABILITY_RANK.get(group.key, len(PROBABILITY_RANK)),
    ),
]


@dataclass
class _Tally:
    label: str
    count: int = 0
    hits: int = 0
    signed: float = 0.0
    events: set | None = None


def _tally(rows: Sequence, classify: _Classify) -> dict[str, _Tally]:
    tallies: dict[str, _Tally] = {}
    for row in rows:
        key, label = classify(row)
        tally = tallies.setdefault(key, _Tally(label, events=set()))
        tally.count += 1
        tally.hits += bool(row.hit)
        tally.signed += row.signed
        tally.events.add(row.event_id)
    return tallies


def _group(key: str, tally: _Tally, z: float) -> Group:
    band = wilson(tally.hits, tally.count, z)
    return Group(
        key, tally.label, tally.count, len(tally.events), tally.hits, tally.signed / tally.count if tally.count else None,
        *(band or (None, None)),
    )


def evaluate(rows: Sequence, horizon: str) -> Evaluation:
    """The breakdowns and the months of the measured outcomes given, the bands widened for every group judged."""
    horizon = horizon if horizon in HORIZON_CHOICES else DEFAULT_HORIZON
    chosen = [each for each in _BREAKDOWNS if each[0] != "horizon" or HORIZON_CHOICES[horizon] is None]
    tallies = [(key, title, _tally(rows, classify), order) for key, title, classify, order in chosen]
    months = _tally(rows, _month)
    total = _tally(rows, lambda _row: ("all", "Alle"))
    every = [tally for _, _, groups, _ in tallies for tally in groups.values()] + list(months.values()) + list(total.values())
    judged = sum(tally.count >= MIN_JUDGED for tally in every)
    z = threshold(judged)
    breakdowns = [
        Breakdown(key, title, sorted((_group(group_key, tally, z) for group_key, tally in groups.items()), key=order))
        for key, title, groups, order in tallies
    ]
    return Evaluation(
        horizon,
        _group("all", total["all"], z) if total else Group("all", "Alle", 0, 0, 0, None, None, None),
        breakdowns,
        [_group(key, months[key], z) for key in sorted(months)],
        judged,
        z,
    )


def evaluation(engine: Engine, horizon: str = DEFAULT_HORIZON) -> Evaluation:
    horizon = horizon if horizon in HORIZON_CHOICES else DEFAULT_HORIZON
    with engine.connect() as connection:
        rows = forecast_repository.measured(connection, HORIZON_CHOICES[horizon])
    return evaluate(rows, horizon)
