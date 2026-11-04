"""The status page (userstories.md, Skalierung und Betriebsqualität, story 1; the admin's decisions of 2026-11-04):
throughput, cost per article, latency, failures, LLM calls, the queue and storage, measured, so hardware and source
decisions rest on measured values (requirements.md, Skalierung und Betriebsqualität, criteria 3 and 4).

- LLM calls: every call of a job is recorded as it ends - its time, its tokens, how it ended, the article it worked for
  where the job names one -, by the client's hook (`OllamaClient.on_call`), never failing the job.
- The job kinds: once an hour the worker stores what each kind did in the hour before - finished, failed, attempts,
  waits and runs - and the queue then, so the history outlasts the finished jobs' deletion. The current hour is read
  from the jobs themselves.
- Articles, their failures and latencies are read from the tables that keep them; latency only of live articles, a
  backfill's work comes behind live work by design.
- Cost per article: the LLM's seconds and tokens of the steps that work for an article, per article they worked for,
  per step and in all; in CHF once the Plattform-Einstellung gives an hour's cost.
- Bottlenecks are named by fixed rules, never one score.
"""
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from app.llm.ollama import LlmCallRecord
from app.repositories import extraction_repository, status_repository
from app.services import platform_settings_service, storage_service
from app.services.job_service import (
    ASSIGN_EVENT_JOB_KIND,
    EXTRACT_EVENTS_JOB_KIND,
    KIND_LABELS,
    RECOGNISE_ENTITIES_JOB_KIND,
    REEXTRACT_EVENTS_JOB_KIND,
    RERECOGNISE_ENTITIES_JOB_KIND,
)

logger = logging.getLogger(__name__)

PERIODS = {"hour": ("Letzte Stunde", timedelta(hours=1)), "day": ("24 Stunden", timedelta(days=1)),
           "week": ("7 Tage", timedelta(days=7)), "month": ("30 Tage", timedelta(days=30))}
DEFAULT_PERIOD = "day"
# How far back the worker fills hours it missed: finished jobs are kept at least a day.
CATCH_UP_HOURS = 48
# The bottleneck rules.
LLM_BUSY = 0.8
QUEUE_WAIT = timedelta(hours=1)
FAILED_SHARE = 0.1
MIN_ATTEMPTS_FOR_SHARE = 10
EXTRACTION_WAITING_LIMIT = 10_000
# The kinds whose calls work for one article, and how the article is found from the payload.
_ARTICLE_KINDS = {
    EXTRACT_EVENTS_JOB_KIND: "article_id", REEXTRACT_EVENTS_JOB_KIND: "article_id",
    RECOGNISE_ENTITIES_JOB_KIND: "extraction_id", RERECOGNISE_ENTITIES_JOB_KIND: "extraction_id",
    ASSIGN_EVENT_JOB_KIND: "candidate_id",
}


def article_of(connection, kind: str, payload: Mapping) -> int | None:
    key = _ARTICLE_KINDS.get(kind)
    if key is None or payload.get(key) is None:
        return None
    if key == "article_id":
        return payload[key]
    if key == "extraction_id":
        return status_repository.article_of_extraction(connection, payload[key])
    return status_repository.article_of_candidate(connection, payload[key])


def recorder(engine: Engine, job) -> Callable[[LlmCallRecord], None]:
    """What records each LLM call of the job: in a transaction of its own, a failure only logged - measuring never
    fails a job."""

    def record(call: LlmCallRecord) -> None:
        try:
            with engine.begin() as connection:
                values = {
                    "called_at": call.called_at, "duration_ms": call.duration_ms, "kind": job.kind, "job_id": job.id,
                    "article_id": article_of(connection, job.kind, job.payload), "model": call.model,
                    "prompt_tokens": call.prompt_tokens, "answer_tokens": call.answer_tokens, "outcome": call.outcome,
                }
                status_repository.insert_llm_call(connection, values)
        except Exception:  # noqa: BLE001 - a measurement must never fail the job it measures
            logger.exception("LLM call not recorded", extra={"job_id": job.id})

    return record


def _hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _hour_rows(connection, start: datetime, now: datetime) -> list[dict]:
    """The rows of one hour: every kind that finished or started a job in it, or has one queued now."""
    stats = status_repository.job_stats(connection, start, start + timedelta(hours=1))
    queued = defaultdict(lambda: [0, None])
    for row in status_repository.queue(connection, now):
        entry = queued[row.kind]
        entry[0] += row.due
        if row.oldest_due_seconds is not None:
            entry[1] = max(entry[1] or 0, row.oldest_due_seconds)
    rows = []
    for kind in sorted(set(stats) | set(queued)):
        values = stats.get(kind, {})
        rows.append({
            "hour": start, "kind": kind, "finished": values.get("finished", 0), "failed": values.get("failed", 0),
            "attempts": values.get("attempts", 0), "failed_attempts": values.get("failed_attempts", 0),
            "started": values.get("started", 0), "wait_seconds": float(values.get("wait_seconds", 0)),
            "wait_seconds_max": float(values.get("wait_seconds_max", 0)), "run_seconds": float(values.get("run_seconds", 0)),
            "run_seconds_max": float(values.get("run_seconds_max", 0)), "queued_due": queued[kind][0] if kind in queued else 0,
            "oldest_due_seconds": float(queued[kind][1]) if kind in queued and queued[kind][1] is not None else None,
        })
    return rows


def measure_hours(engine: Engine, now: datetime | None = None) -> int:
    """Store the hours over since the last one stored - at most CATCH_UP_HOURS back -; how many rows. The worker calls
    it in its loop."""
    now = now or datetime.now(UTC)
    current = _hour(now)
    stored = 0
    with engine.begin() as connection:
        latest = status_repository.latest_hour(connection)
        start = max(latest + timedelta(hours=1) if latest else current - timedelta(hours=1), current - timedelta(hours=CATCH_UP_HOURS))
        while start < current:
            stored += status_repository.insert_job_hours(connection, _hour_rows(connection, start, now))
            start += timedelta(hours=1)
    if stored:
        logger.info("Job hours measured", extra={"rows": stored})
    return stored


# Views


def duration_label(seconds: float | None) -> str:
    """3 s, 4,5 min, 2,1 h, 1,5 d; – for none."""
    if seconds is None:
        return "–"
    for limit, unit, size in ((90, "s", 1), (90 * 60, "min", 60), (36 * 3600, "h", 3600)):
        if seconds < limit:
            value = seconds / size
            return f"{value:.0f} {unit}" if unit == "s" or value >= 10 else f"{value:.1f} {unit}".replace(".", ",")
    return f"{seconds / 86400:.1f} d".replace(".", ",")


def count_label(value: float | None, places: int = 0) -> str:
    if value is None:
        return "–"
    text = f"{value:,.{places}f}".replace(",", "'").replace(".", ",")
    return text


def share_label(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f} %" if whole else "–"


@dataclass(frozen=True)
class Latency:
    key: str
    label: str
    count: int
    median: float | None
    p90: float | None

    @property
    def median_label(self) -> str:
        return duration_label(self.median)

    @property
    def p90_label(self) -> str:
        return duration_label(self.p90)


LATENCY_LABELS = {
    "normalized": "Abruf bis normalisiert (mit Volltext)",
    "extracted": "Normalisiert bis extrahiert",
    "assigned": "Extrahiert bis einem Ereignis zugeordnet",
    "total": "Abruf bis Ereignis, gesamt",
}


@dataclass(frozen=True)
class LlmStep:
    kind: str
    calls: int
    seconds: float
    prompt_tokens: int
    answer_tokens: int
    unanswered: int
    per_article_seconds: float | None  # over the articles its calls worked for
    per_article_tokens: float | None
    cost_rappen: float | None  # per article

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


@dataclass(frozen=True)
class KindFigures:
    kind: str
    finished: int
    failed: int
    attempts: int
    failed_attempts: int
    started: int
    wait_mean: float | None
    wait_max: float | None
    run_mean: float | None
    run_max: float | None

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def failed_share(self) -> float | None:
        return self.failed_attempts / self.attempts if self.attempts else None

    @property
    def failed_label(self) -> str:
        return share_label(self.failed_attempts, self.attempts)


@dataclass(frozen=True)
class QueueRow:
    kind: str
    priority: int
    due: int
    later: int
    running: int
    oldest_due_seconds: float | None

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind) + (" (Backfill)" if self.priority else "")

    @property
    def oldest_label(self) -> str:
        return duration_label(self.oldest_due_seconds)


@dataclass(frozen=True)
class SourceRow:
    source_id: int | None
    name: str
    articles: int
    per_hour: float
    duplicates: int
    failed: int
    with_event: int
    llm_articles: int
    llm_seconds: float

    @property
    def duplicate_label(self) -> str:
        return share_label(self.duplicates, self.articles)

    @property
    def failed_label(self) -> str:
        return share_label(self.failed, self.articles)

    @property
    def event_label(self) -> str:
        return share_label(self.with_event, self.articles)

    @property
    def llm_label(self) -> str:
        return duration_label(self.llm_seconds / self.llm_articles) if self.llm_articles else "–"


@dataclass(frozen=True)
class HistoryRow:
    start: datetime
    articles: int
    failed_articles: int
    llm_calls: int
    llm_seconds: float
    busy: float | None
    attempts: int
    failed_attempts: int
    queued_due: int | None


@dataclass(frozen=True)
class Status:
    period: str
    period_label: str
    start: datetime
    end: datetime
    articles: object
    per_hour: float
    extracted: int
    llm_steps: list[LlmStep]
    llm_calls: int
    llm_seconds: float
    llm_busy: float
    llm_slots: int
    prompt_tokens: int
    answer_tokens: int
    per_article_seconds: float | None
    per_article_tokens: float | None
    cost_rappen_per_hour: int
    kinds: list[KindFigures]
    attempts: int
    failed_attempts: int
    latencies: list[Latency]
    queue: list[QueueRow]
    extraction_waiting: int
    sources: list[SourceRow]
    history: list[HistoryRow]
    history_unit: str
    storage: object  # storage_service.Measurement or None
    growth: object  # storage_service.Growth or None
    bottlenecks: list[str]

    @property
    def periods(self) -> dict[str, str]:
        return {key: label for key, (label, _) in PERIODS.items()}

    @property
    def cost_per_article_label(self) -> str | None:
        if not self.cost_rappen_per_hour or self.per_article_seconds is None:
            return None
        return chf_label(self.per_article_seconds / 3600 * self.cost_rappen_per_hour)

    @property
    def failed_attempt_label(self) -> str:
        return share_label(self.failed_attempts, self.attempts)


def chf_label(rappen: float) -> str:
    """CHF 0.0123 for small amounts, CHF 12.30 above a franc."""
    francs = rappen / 100
    return f"CHF {francs:.4f}" if francs < 1 else f"CHF {francs:.2f}"


def bottlenecks(busy: float, slots: int, queue: list[QueueRow], kinds: list[KindFigures], waiting: int, budget: int, used: int) -> list[str]:
    """The bottlenecks the measured values show, each by a fixed rule; none: an empty list."""
    found = []
    if busy >= LLM_BUSY:
        found.append(f"LLM ausgelastet: {busy * 100:.0f} % der Zeit von {slots} {'Platz' if slots == 1 else 'Plätzen'} belegt.")
    for row in queue:
        if row.oldest_due_seconds is not None and row.oldest_due_seconds >= QUEUE_WAIT.total_seconds():
            found.append(f"Warteschlange: der älteste fällige Job «{row.label}» wartet seit {row.oldest_label} ({row.due} fällig).")
    for each in kinds:
        share = each.failed_share
        if share is not None and each.attempts >= MIN_ATTEMPTS_FOR_SHARE and share > FAILED_SHARE:
            found.append(f"Fehler: «{each.label}» scheitert bei {each.failed_label} der Versuche ({each.failed_attempts} von {each.attempts}).")
    if waiting and used >= budget:
        more = "über " if waiting >= EXTRACTION_WAITING_LIMIT else ""
        found.append(f"Extraktionsbudget ausgeschöpft: {more}{waiting} Artikel warten ({used} von {budget} pro Stunde).")
    return found


def _kind_figures(rows: list[dict]) -> list[KindFigures]:
    total: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        values = total[row["kind"]]
        for key in ("finished", "failed", "attempts", "failed_attempts", "started", "wait_seconds", "run_seconds"):
            values[key] += row[key]
        values["wait_seconds_max"] = max(values["wait_seconds_max"], row["wait_seconds_max"])
        values["run_seconds_max"] = max(values["run_seconds_max"], row["run_seconds_max"])
    figures = []
    for kind, values in total.items():
        if not (values["finished"] or values["started"]):
            continue
        figures.append(KindFigures(
            kind, int(values["finished"]), int(values["failed"]), int(values["attempts"]), int(values["failed_attempts"]),
            int(values["started"]), values["wait_seconds"] / values["started"] if values["started"] else None,
            values["wait_seconds_max"] if values["started"] else None,
            values["run_seconds"] / values["finished"] if values["finished"] else None,
            values["run_seconds_max"] if values["finished"] else None,
        ))
    return sorted(figures, key=lambda each: each.label.lower())


def overview(engine: Engine, period: str = DEFAULT_PERIOD, now: datetime | None = None) -> Status:
    period = period if period in PERIODS else DEFAULT_PERIOD
    label, span = PERIODS[period]
    end = now or datetime.now(UTC)
    start = end - span
    unit = "hour" if span <= timedelta(days=1) else "day"
    current_hour = _hour(end)
    with engine.connect() as connection:
        settings = platform_settings_service.load(connection)
        articles = status_repository.articles(connection, start, end)
        extracted = status_repository.extracted_articles(connection, start, end)
        llm = status_repository.llm_by_kind(connection, start, end)
        llm_articles = status_repository.llm_articles(connection, start, end, _ARTICLE_KINDS)
        stored = [dict(row._mapping) for row in status_repository.job_hours(connection, _hour(start), current_hour)]
        stored_hours = {row["hour"] for row in stored}
        # The hours not stored yet - the current one, any the worker missed - from the jobs themselves.
        live = []
        hour = max(_hour(start), current_hour - timedelta(hours=CATCH_UP_HOURS))
        while hour <= current_hour:
            if hour not in stored_hours:
                live += _hour_rows(connection, hour, end)
            hour += timedelta(hours=1)
        latency_rows = status_repository.latencies(connection, start, end)
        queue_rows = status_repository.queue(connection, end)
        waiting = len(extraction_repository.due(
            connection, EXTRACT_EVENTS_JOB_KIND, settings.extraction_language_codes, settings.extraction_min_text_chars,
            settings.extraction_max_age_days, EXTRACTION_WAITING_LIMIT,
        ))
        used = extraction_repository.jobs_since(connection, EXTRACT_EVENTS_JOB_KIND, timedelta(hours=1))
        sources = status_repository.by_source(connection, start, end)
        article_buckets = {row.bucket: row for row in status_repository.articles_by_bucket(connection, start, end, unit)}
        llm_buckets = {row.bucket: row for row in status_repository.llm_by_bucket(connection, start, end, unit)}
    hours = span.total_seconds() / 3600
    slots = settings.llm_max_parallel_calls
    llm_seconds = sum((row.ms or 0) for row in llm) / 1000
    prompt_tokens = sum(row.prompt_tokens or 0 for row in llm)
    answer_tokens = sum(row.answer_tokens or 0 for row in llm)
    article_seconds = sum((row.ms or 0) for row in llm if row.kind in _ARTICLE_KINDS) / 1000
    article_tokens = sum((row.prompt_tokens or 0) + (row.answer_tokens or 0) for row in llm if row.kind in _ARTICLE_KINDS)
    rate = settings.llm_cost_rappen_per_hour
    def per_article(row) -> bool:
        return row.kind in _ARTICLE_KINDS and row.articles > 0

    # Per article its calls worked for - not per article extracted: calls before the recording began are unknown.
    steps = [
        LlmStep(
            row.kind, row.calls, (row.ms or 0) / 1000, row.prompt_tokens or 0, row.answer_tokens or 0, row.unanswered,
            (row.ms or 0) / 1000 / row.articles if per_article(row) else None,
            ((row.prompt_tokens or 0) + (row.answer_tokens or 0)) / row.articles if per_article(row) else None,
            (row.ms or 0) / 1000 / row.articles / 3600 * rate if rate and per_article(row) else None,
        )
        for row in llm
    ]
    job_rows = stored + live
    kinds = _kind_figures(job_rows)
    queue = [QueueRow(row.kind, row.priority, row.due, row.later, row.running, row.oldest_due_seconds) for row in queue_rows]
    busy = llm_seconds / (span.total_seconds() * slots) if slots else 0.0
    by_bucket: dict[datetime, list[dict]] = defaultdict(list)
    for row in job_rows:
        hour_utc = row["hour"].astimezone(UTC)
        key = hour_utc if unit == "hour" else hour_utc.replace(hour=0)
        by_bucket[key].append(row)
    bucket_span = 3600 if unit == "hour" else 86400
    first = _hour(start) if unit == "hour" else _hour(start).replace(hour=0)
    history = []
    moment = first
    while moment <= end:
        a, l, j = article_buckets.get(moment), llm_buckets.get(moment), by_bucket.get(moment, [])
        seconds = (l.ms or 0) / 1000 if l else 0.0
        # The queue at the bucket's last measurement.
        latest_hour = max((row["hour"] for row in j), default=None)
        queued = sum(row["queued_due"] for row in j if row["hour"] == latest_hour) if latest_hour else None
        history.append(HistoryRow(
            moment, a.articles if a else 0, a.failed if a else 0, l.calls if l else 0, seconds,
            seconds / (bucket_span * slots) if slots else None, sum(row["attempts"] for row in j),
            sum(row["failed_attempts"] for row in j), queued,
        ))
        moment += timedelta(seconds=bucket_span)
    history.reverse()
    measurements = storage_service.history(engine)
    return Status(
        period, label, start, end, articles, (articles.live + articles.backfill) / hours, extracted, steps,
        sum(row.calls for row in llm), llm_seconds, busy, slots, prompt_tokens, answer_tokens,
        article_seconds / llm_articles if llm_articles else None, article_tokens / llm_articles if llm_articles else None, rate, kinds,
        sum(each.attempts for each in kinds), sum(each.failed_attempts for each in kinds),
        [Latency(key, LATENCY_LABELS[key], row.count, row.median, row.p90) for key, row in latency_rows.items()],
        queue, waiting,
        [SourceRow(row.source_id, row.name or "Gelöschte Quelle", row.articles, row.articles / hours, row.duplicates, row.failed,
                   row.with_event, row.llm_articles, row.llm_ms / 1000) for row in sources],
        history, unit, measurements[0] if measurements else None, storage_service.growth(measurements, 7),
        bottlenecks(busy, slots, queue, kinds, waiting, settings.extractions_per_hour, used),
    )
