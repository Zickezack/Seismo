"""Event extraction (userstories.md, Strukturierte Ereignisextraktion, story 1): an LLM turns an article into event
candidates whose every statement is backed by a quote from the article.

Only articles that pass the pre-filter reach the model (requirements.md, non-goal 6: no uncontrolled processing of
every article): originals of a source that extracts (Quelle), in a listed language, with enough text, not too old, never
a later copy of a linked article - and at most a number per hour, newest first (Plattform-Einstellungen). The worker
queues them once a minute; the job calls Ollama outside any transaction and writes the result in the transaction that
completes it. A statement whose quote is not in the text the model saw is not stored, an event left without one
neither: no evidence, no statement (datamodel.md, rule 2).
"""
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import Connection, Engine

from app.db.models import ConfidenceLevel, EventStatus, EventType, StatementKind, ValidationResult
from app.jobs.errors import JobInterrupted
from app.llm import event_schema
from app.llm.event_schema import Direction, Horizon, TargetKind
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import MAX_ANSWER_TOKENS, LlmAnswer, OllamaClient
from app.repositories import extraction_repository
from app.services import job_service, platform_settings_service, similarity_service

logger = logging.getLogger(__name__)

BUDGET_PERIOD = timedelta(hours=1)
# Of an unusable answer, kept for diagnosis: its start shows what went wrong.
MAX_RAW_ANSWER_CHARS = 20_000
# Tokens of the context kept from the article's text: the instructions with the article's header, and the answer.
RESERVED_TOKENS = 1000 + MAX_ANSWER_TOKENS
# A shorter quote backs nothing: a name or a number is in many sentences. In a script without spaces between words
# a character says about as much as a word: "美國期中選舉過後", after the US midterm elections.
MIN_QUOTE_CHARS = 10
MIN_UNSPACED_QUOTE_CHARS = 4
# Typographic quotes and dashes a model writes as plain ones, and the other way round.
_PLAIN_MARKS = str.maketrans({"“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‘": "'", "’": "'", "‚": "'", "–": "-", "—": "-"})
_QUOTE_MARKS = " \"'"
_QUOTE_ENDS = _QUOTE_MARKS + ".,;:"

SYSTEM_PROMPT = f"""You extract structured events from one news article for an event database. Answer only with JSON
in the given schema.

- An event is something that happened, is happening or is announced in the real world: an attack, an earthquake,
  a rate decision, a strike, a sanction, a merger, a data release. Report at most {event_schema.MAX_EVENTS} distinct
  events, the most important first; none for an article that reports no event (an opinion piece without news, an
  advert, a list of unrelated headlines).
- Write every text of your own in English, whatever the article's language: titles, summaries, roles,
  descriptions, statements and time notes. Only quotes stay in the article's language.
- Statements: up to {event_schema.MAX_STATEMENTS} per event, the most material ones, each one fact in one short
  sentence of your own words - not a copy of its quote, not a repetition of another statement.
- Every statement needs a quote: the passage of the article that states it, copied character by character in the
  article's language - not translated, not shortened, without "...". As short as possible, at most one sentence.
  Leave out a statement you cannot back with such a quote.
- Every detail needs a quote as well, copied the same way: location.quote the passage that names the place,
  time_quote the one that states the time, each actor's quote the one that names them, each impact's quote the one
  that states it. Without such a passage leave place, country and time null and leave out the actor or impact.
- confidence of a statement - how faithfully it renders its quote: high when the quote says it plainly, medium when
  it says it with some reading between the lines, low when it only hints at it. confidence of an event - how sure
  its type, status and impacts follow from the article: high, medium or low.
- Statement kinds: fact - the article reports it as having happened; claim - someone says it, unverified ("X said");
  source_forecast - a prediction made in the article or by someone it quotes; interpretation - the article's own
  assessment or analysis.
- Never guess. A field the article does not state stays null or empty. time_note: when the event happens, in a few
  English words as the article states it, e.g. "on Friday morning" or "effective Nov. 7". location.country as
  ISO 3166-1 alpha-2 code, only for a place inside one country - null for a region across borders, the world or a
  market; location.place as the article names it.
- event_type: one of {", ".join(EventType)}. trade_policy covers tariffs, sanctions and export controls,
  economic_data the release of statistics.
- status: start (it begins), update (it goes on, new details), escalation, closure (it ends), resumption (it starts
  again), unknown.
- actors: who acts or is directly concerned, with their role in the event.
- impacts: only effects the article itself names, on companies, sectors, commodities, countries or markets, with
  direction and horizon; none of your own."""

EVENT_TYPE_LABELS = {
    EventType.ARMED_CONFLICT: "Bewaffneter Konflikt",
    EventType.TERRORISM: "Terroranschlag",
    EventType.CIVIL_UNREST: "Unruhen und Proteste",
    EventType.NATURAL_DISASTER: "Naturkatastrophe",
    EventType.ACCIDENT: "Unfall",
    EventType.CYBER_INCIDENT: "Cybervorfall",
    EventType.INFRASTRUCTURE_OUTAGE: "Infrastrukturausfall",
    EventType.SUPPLY_CHAIN_DISRUPTION: "Lieferkettenstörung",
    EventType.ENERGY_SUPPLY: "Energieversorgung",
    EventType.COMMODITY_MARKET: "Rohstoffmarkt",
    EventType.MONETARY_POLICY: "Geldpolitik",
    EventType.FISCAL_POLICY: "Fiskalpolitik",
    EventType.TRADE_POLICY: "Handelspolitik und Sanktionen",
    EventType.REGULATION: "Regulierung",
    EventType.ELECTION_POLITICS: "Wahlen und Politik",
    EventType.DIPLOMACY: "Diplomatie",
    EventType.CORPORATE: "Unternehmen",
    EventType.LABOR: "Arbeitskampf",
    EventType.HEALTH: "Gesundheit",
    EventType.LEGAL: "Justiz",
    EventType.ECONOMIC_DATA: "Wirtschaftsdaten",
    EventType.OTHER: "Sonstiges",
}
STATUS_LABELS = {
    EventStatus.START: "Beginn",
    EventStatus.UPDATE: "Fortsetzung",
    EventStatus.ESCALATION: "Eskalation",
    EventStatus.CLOSURE: "Abschluss",
    EventStatus.RESUMPTION: "Wiederaufnahme",
    EventStatus.UNKNOWN: "Status unbekannt",
}
KIND_LABELS = {
    StatementKind.FACT: "Tatsache",
    StatementKind.CLAIM: "Behauptung",
    StatementKind.SOURCE_FORECAST: "Quellenprognose",
    StatementKind.INTERPRETATION: "Interpretation",
}
TARGET_KIND_LABELS = {
    TargetKind.COMPANY: "Unternehmen",
    TargetKind.SECTOR: "Branche",
    TargetKind.COMMODITY: "Rohstoff",
    TargetKind.COUNTRY: "Land",
    TargetKind.MARKET: "Markt",
    TargetKind.OTHER: "Sonstiges",
}
DIRECTION_LABELS = {Direction.POSITIVE: "positiv", Direction.NEGATIVE: "negativ", Direction.MIXED: "gemischt", Direction.UNCLEAR: "unklar"}
HORIZON_LABELS = {
    Horizon.DAYS: "Tage",
    Horizon.WEEKS: "Wochen",
    Horizon.MONTHS: "Monate",
    Horizon.YEARS: "Jahre",
    Horizon.UNKNOWN: "Horizont unbekannt",
}


class InvalidAnswer(Exception):
    """The model's answer was outside the schema or cut, also when asked again: retried with the job."""


@dataclass(frozen=True)
class Failure:
    """Why an answer is not usable: its validation result and what was wrong (no value of the article's)."""

    result: ValidationResult
    errors: str

    @property
    def problem(self) -> str:
        """What the model is told when asked again."""
        if self.result == ValidationResult.INCOMPLETE:
            return "it was cut off before its end - answer shorter, with fewer events and statements"
        return f"it did not match the schema:\n{self.errors}"


def enqueue_due(engine: Engine) -> list[int]:
    """Queue an extraction for the newest articles that pass the pre-filter, as many as the hour's budget leaves.
    The worker calls it only while Ollama serves the model (OllamaClient.serves_model)."""
    kind = job_service.EXTRACT_EVENTS_JOB_KIND
    with engine.begin() as connection:
        settings = platform_settings_service.load(connection)
        left = settings.extractions_per_hour - extraction_repository.jobs_since(connection, kind, BUDGET_PERIOD)
        if left <= 0:
            return []
        due = extraction_repository.due(
            connection, kind, settings.extraction_language_codes, settings.extraction_min_text_chars,
            settings.extraction_max_age_days, left,
        )
        job_ids = [
            job_service.enqueue(connection, kind, {"article_id": article_id}, dedupe_key=f"{kind}:{article_id}")
            for article_id in due
        ]
    return [job_id for job_id in job_ids if job_id is not None]


def extract(
    engine: Engine, article_id: int, client: OllamaClient, limiter: LlmCallLimiter, stop_event: threading.Event | None = None
) -> Callable[[Connection], None] | None:
    """First phase of an extraction job: ask the model. Returns the effect that stores the candidates, or None when
    the article has an extraction already, became a duplicate or lost its text meanwhile.

    An answer outside the schema or cut is stored as a run of its own at once, and the model asked again in the same
    slot, told what was wrong; a second such answer raises InvalidAnswer. LlmError (app/llm/ollama.py) when the
    model brought no answer. Both are retried with the job. A worker that is stopping asks no follow-up question - it
    would outlast its stop_grace_period (docker-compose.yml): JobInterrupted puts the job back into the queue."""
    with engine.connect() as connection:
        article = extraction_repository.get_for_extraction(connection, article_id)
    if article is None or article.extracted or article.duplicate_of_id is not None or not article.plain_text:
        return None
    text, truncated = fit_text(article.plain_text, client.num_ctx)
    run = {"article_id": article_id, "schema_version": event_schema.SCHEMA_VERSION, "input_chars": len(text), "truncated": truncated}
    started = time.perf_counter()
    with limiter.slot():
        answer, parsed, failure = _ask(client, messages(article, text))
        repaired = failure is not None
        if failure:
            _record(engine, run, answer, failure, started, repaired=False)
            if stop_event is not None and stop_event.is_set():
                raise JobInterrupted("Worker stoppt: keine Nachfrage beim Modell")
            answer, parsed, failure = _ask(client, messages(article, text, failure))
            if failure:
                _record(engine, run, answer, failure, started, repaired=True)
                raise InvalidAnswer(f"Antwort auch auf Nachfrage {_RESULT_LABELS[failure.result]}: {failure.errors.splitlines()[0]}")
    duration_ms = _since(started)
    quality = source_quality(article.url, article.source_quality, platform_settings_service.current(engine).source_quality_levels)
    candidates, dropped, details_dropped = backed_candidates(parsed, text, lowered=truncated or repaired, quality=quality)
    values = run | {"model": answer.model, "duration_ms": duration_ms, "statements_dropped": dropped, "repaired": repaired}

    def store(connection: Connection) -> None:
        if extraction_repository.insert_extraction(connection, values, candidates) is not None:
            # IDs and counts only (datamodel.md, rule 13).
            fields = {
                "article_id": article_id, "candidates": len(candidates), "statements_dropped": dropped,
                "details_dropped": details_dropped, "repaired": repaired, "duration_ms": duration_ms,
                "prompt_tokens": answer.prompt_tokens, "answer_tokens": answer.answer_tokens,
            }
            logger.info("Events extracted", extra=fields)

    return store


_RESULT_LABELS = {ValidationResult.INVALID: "ausserhalb des Schemas", ValidationResult.INCOMPLETE: "abgeschnitten"}


def _ask(client: OllamaClient, chat: list[dict]) -> tuple[LlmAnswer, event_schema.ExtractionAnswer | None, Failure | None]:
    answer = client.chat_json(chat, event_schema.json_schema())
    if not answer.complete:
        return answer, None, Failure(ValidationResult.INCOMPLETE, f"cut off after {answer.answer_tokens} tokens")
    try:
        return answer, event_schema.parse(answer.content), None
    except ValidationError as error:
        return answer, None, Failure(ValidationResult.INVALID, event_schema.error_summary(error))


def _record(engine: Engine, run: dict, answer: LlmAnswer, failure: Failure, started: float, repaired: bool) -> None:
    """An unusable answer, stored at once in its own transaction - whatever becomes of the job - and logged by its
    kind and number of errors only (datamodel.md, rule 13)."""
    values = run | {
        "model": answer.model,
        "duration_ms": _since(started),
        "validation_result": failure.result,
        "validation_errors": failure.errors,
        "raw_answer": answer.content[:MAX_RAW_ANSWER_CHARS],
        "repaired": repaired,
    }
    with engine.begin() as connection:
        extraction_repository.insert_run(connection, values)
    fields = {
        "article_id": run["article_id"], "result": failure.result, "errors": len(failure.errors.splitlines()),
        "follow_up": repaired, "answer_tokens": answer.answer_tokens,
    }
    logger.warning("Extraction answer not usable", extra=fields)


def _since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


# After the article: a model follows the last instruction it read most closely, and mirrors the article's language.
REMINDER = "Answer in English - titles, summaries, roles, descriptions, statements, time notes. Quotes verbatim."


def user_message(article, text: str, failure: Failure | None = None) -> str:
    """The article for the model; asked again, with what was wrong with its answer before - not that answer itself,
    which would not fit the context next to the text."""
    published = article.published_at.date().isoformat() if article.published_at else "unknown"
    header = f"Source: {article.source_name}\nPublished: {published}\nTitle: {article.title or 'unknown'}"
    message = f"{header}\n\nArticle text:\n{text}\n\n{REMINDER}"
    if failure:
        message += f"\n\nA previous answer was not usable: {failure.problem}\nAnswer again: the complete JSON in the schema."
    return message


def messages(article, text: str, failure: Failure | None = None) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message(article, text, failure)}]


def fit_text(text: str, num_ctx: int) -> tuple[str, bool]:
    """The text cut to the context left after RESERVED_TOKENS, at a word's end, and whether it was cut. Counted
    cautiously: a character outside ASCII as a token of its own (a CJK character is about one), three ASCII ones as one."""
    budget = num_ctx - RESERVED_TOKENS
    tokens = 0.0
    for index, char in enumerate(text):
        tokens += 1 / 3 if char.isascii() else 1
        if tokens > budget:
            cut = max(text.rfind(" ", 0, index), text.rfind("\n", 0, index))
            return text[: cut if cut > 0 else index].rstrip(), True
    return text, False


def find_quote(quote: str, text: str) -> str | None:
    """The passage of the text the quote copies, as it stands there; None when the text does not contain it, or it is
    too short to back anything.

    Compared without regard to spaces, typographic quotes and dashes, and case - and without the quotation marks and
    punctuation at the quote's ends: a model sets marks around one sentence of a longer quoted passage and starts it
    with a capital."""
    quote = "".join(map(_plain, " ".join(quote.split())))
    plain, positions = [], []
    for index, char in enumerate(text):
        if char.isspace():
            if plain and plain[-1] == " ":
                continue
            char = " "
        plain.append(_plain(char))
        positions.append(index)
    plain = "".join(plain)
    # With its own punctuation at the end first, so the passage ends as the sentence does.
    shortest = MIN_UNSPACED_QUOTE_CHARS if similarity_service.unspaced(quote) else MIN_QUOTE_CHARS
    for needle in (quote.strip(_QUOTE_MARKS), quote.strip(_QUOTE_ENDS)):
        start = plain.find(needle) if len(needle) >= shortest else -1
        if start >= 0:
            return text[positions[start] : positions[start + len(needle) - 1] + 1]
    return None


def _plain(char: str) -> str:
    """A character as quotes are compared: plain marks, lower case where that keeps it one character."""
    char = char.translate(_PLAIN_MARKS)
    lower = char.lower()
    return lower if len(lower) == 1 else char


def backed_candidates(
    answer: event_schema.ExtractionAnswer, text: str, lowered: bool = False, quality: str | None = None
) -> tuple[list[dict], int, int]:
    """The candidates as rows, everything with the passage it quotes as it stands in the text. A statement without a
    found quote is dropped, and a candidate left without one; an actor or impact without one is left out, place and
    country or the time without one are unknown (datamodel.md, rule 5). `lowered`: the text was cut or the answer
    repaired - every statement's extraction level one lower. `quality`: the source's, for every quote.

    Returns the rows, how many statements and how many details (place, time, actor, impact) were dropped."""
    candidates, dropped, details_dropped = [], 0, 0
    for event in answer.events:
        statements = []
        for statement in event.statements:
            quote = find_quote(statement.quote, text)
            if quote is None:
                dropped += 1
                continue
            level = lower(statement.confidence) if lowered else statement.confidence
            statements.append({"kind": statement.kind, "text": statement.text, "quote": quote, "confidence": level,
                               "source_quality": quality})
        if not statements:
            continue
        location_quote = _detail_quote(event.location.quote, text) if event.location.place or event.location.country else None
        time_quote = _detail_quote(event.time_quote, text) if event.time_note else None
        actors = [actor.model_dump(mode="json") | {"quote": found} for actor in event.actors if (found := _detail_quote(actor.quote, text))]
        impacts = [impact.model_dump(mode="json") | {"quote": found} for impact in event.impacts if (found := _detail_quote(impact.quote, text))]
        details_dropped += (len(event.actors) - len(actors)) + (len(event.impacts) - len(impacts))
        details_dropped += bool((event.location.place or event.location.country) and not location_quote) + bool(event.time_note and not time_quote)
        candidates.append({
            "event_type": event.event_type,
            "status": event.status,
            "inference_confidence": event.confidence,
            "title": event.title,
            "summary": event.summary,
            "country": event.location.country if location_quote else None,
            "place": event.location.place if location_quote else None,
            "location_quote": location_quote,
            "time_note": event.time_note if time_quote else None,
            "time_quote": time_quote,
            "actors": actors,
            "impacts": impacts,
            "statements": statements,
        })
    return candidates, dropped, details_dropped


def _detail_quote(quote: str | None, text: str) -> str | None:
    return find_quote(quote, text) if quote else None


def lower(level: ConfidenceLevel) -> ConfidenceLevel:
    """One level lower; low stays low."""
    return {ConfidenceLevel.HIGH: ConfidenceLevel.MEDIUM}.get(level, ConfidenceLevel.LOW)


def source_quality(url: str, source_level: str | None, domain_levels: dict[str, str]) -> str | None:
    """The quality of the source an article is from: the most specific listed domain its host is or lies under, else
    its Quelle's, else unknown (None)."""
    host = (urlsplit(url).hostname or "").lower()
    matching = [domain for domain in domain_levels if host == domain or host.endswith(f".{domain}")]
    return domain_levels[max(matching, key=len)] if matching else source_level


LEVEL_LABELS = {ConfidenceLevel.HIGH: "hoch", ConfidenceLevel.MEDIUM: "mittel", ConfidenceLevel.LOW: "niedrig"}
UNKNOWN = "unbekannt"
RESULT_LABELS = {ValidationResult.INVALID: "Ausserhalb des Schemas", ValidationResult.INCOMPLETE: "Abgeschnitten"}


def level_label(level: str | None) -> str:
    return LEVEL_LABELS[level] if level else UNKNOWN


@dataclass(frozen=True)
class StatementView:
    kind: str
    text: str
    quote: str
    extraction_confidence: str | None = None  # None before schema 2
    source_quality: str | None = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def levels_label(self) -> str:
        """The two levels of the statement, never combined (datamodel.md, rule 7)."""
        return f"Extraktion {level_label(self.extraction_confidence)} · Quelle {level_label(self.source_quality)}"


@dataclass(frozen=True)
class CandidateView:
    id: int
    event_type: str
    status: str
    title: str
    summary: str
    country: str | None
    place: str | None
    time_note: str | None
    actors: list[dict]
    impacts: list[dict]
    statements: list[StatementView] = field(default_factory=list)
    location_quote: str | None = None
    time_quote: str | None = None
    inference_confidence: str | None = None  # None before schema 2

    @property
    def type_label(self) -> str:
        return EVENT_TYPE_LABELS[self.event_type]

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def where(self) -> str:
        """The place as the article names it - else unknown, never guessed (datamodel.md, rule 5)."""
        return ", ".join(part for part in (self.place, self.country) if part) or f"Ort {UNKNOWN}"

    @property
    def when(self) -> str:
        return self.time_note or f"Zeit {UNKNOWN}"

    @property
    def inference_label(self) -> str:
        return f"Schlussfolgerung {level_label(self.inference_confidence)}"

    @property
    def impact_lines(self) -> list[tuple[str, str | None]]:
        """Each impact as a line, with the passage that states it (None before schema 2)."""
        return [
            (
                f"{impact['target']} ({TARGET_KIND_LABELS[impact['target_kind']]}): {DIRECTION_LABELS[impact['direction']]}, "
                f"{HORIZON_LABELS[impact['horizon']]}" + (f" - {impact['description']}" if impact["description"] else ""),
                impact.get("quote"),
            )
            for impact in self.impacts
        ]


@dataclass(frozen=True)
class RunView:
    """A run whose answer was not usable."""

    extracted_at: datetime
    model: str
    validation_result: str
    validation_errors: str | None
    repaired: bool  # the answer to the follow-up question

    @property
    def result_label(self) -> str:
        return RESULT_LABELS[self.validation_result] + (" (auf Nachfrage)" if self.repaired else "")


@dataclass(frozen=True)
class ExtractionView:
    model: str
    schema_version: str
    extracted_at: datetime
    duration_ms: int
    input_chars: int
    truncated: bool
    statements_dropped: int
    candidates: list[CandidateView]
    repaired: bool = False


@dataclass(frozen=True)
class LlmStatus:
    """The LLM server as the environment names it (OLLAMA_*), and whether it serves the model now."""

    base_url: str
    model: str
    num_ctx: int
    served: bool


# The settings page waits at most this long for the answer.
STATUS_TIMEOUT_SECONDS = 3


def llm_status(base_url: str, model: str, num_ctx: int) -> LlmStatus | None:
    """None while no server is configured."""
    if not base_url:
        return None
    with OllamaClient(base_url, model, num_ctx) as client:
        return LlmStatus(base_url, model, num_ctx, client.serves_model(STATUS_TIMEOUT_SECONDS))


def latest_for_article(engine: Engine, article_id: int) -> ExtractionView | None:
    """The article's latest extraction with a valid answer as its page shows it; None before the first."""
    with engine.connect() as connection:
        extraction = extraction_repository.latest(connection, article_id)
        if extraction is None:
            return None
        candidates = extraction_repository.candidates_of(connection, extraction.id)
        statements = extraction_repository.statements_of(connection, extraction.id)
    by_candidate: dict[int, list[StatementView]] = {}
    for row in statements:
        view = StatementView(row.kind, row.text, row.quote, row.extraction_confidence, row.source_quality)
        by_candidate.setdefault(row.candidate_id, []).append(view)
    views = [
        CandidateView(
            id=row.id, event_type=row.event_type, status=row.status, title=row.title, summary=row.summary,
            country=row.country, place=row.place, time_note=row.time_note, actors=row.actors, impacts=row.impacts,
            statements=by_candidate.get(row.id, []), location_quote=row.location_quote, time_quote=row.time_quote,
            inference_confidence=row.inference_confidence,
        )
        for row in candidates
    ]
    return ExtractionView(
        model=extraction.model, schema_version=extraction.schema_version, extracted_at=extraction.extracted_at,
        duration_ms=extraction.duration_ms, input_chars=extraction.input_chars, truncated=extraction.truncated,
        statements_dropped=extraction.statements_dropped, candidates=views, repaired=extraction.repaired,
    )


def unusable_runs_for_article(engine: Engine, article_id: int) -> list[RunView]:
    """The article's runs whose answer was not usable, the latest first."""
    with engine.connect() as connection:
        rows = extraction_repository.unusable_runs(connection, article_id)
    return [RunView(row.extracted_at, row.model, row.validation_result, row.validation_errors, row.repaired) for row in rows]
