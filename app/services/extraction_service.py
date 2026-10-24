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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine

from app.db.models import EventStatus, EventType, StatementKind
from app.llm import event_schema
from app.llm.event_schema import Direction, Horizon, TargetKind
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import MAX_ANSWER_TOKENS, OllamaClient
from app.repositories import extraction_repository
from app.services import job_service, platform_settings_service

logger = logging.getLogger(__name__)

BUDGET_PERIOD = timedelta(hours=1)
# Tokens of the context kept from the article's text: the instructions with the article's header, and the answer.
RESERVED_TOKENS = 1000 + MAX_ANSWER_TOKENS
# A shorter quote backs nothing: a name or a number is in many sentences.
MIN_QUOTE_CHARS = 10
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


class IncompleteAnswer(Exception):
    """The model's answer was cut before its end: its JSON is incomplete. Retried like a failed call."""


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
    engine: Engine, article_id: int, client: OllamaClient, limiter: LlmCallLimiter
) -> Callable[[Connection], None] | None:
    """First phase of an extraction job: ask the model. Returns the effect that stores the candidates, or None when
    the article has an extraction already, became a duplicate or lost its text meanwhile.

    Raises LlmError (app/llm/ollama.py) when the model brought no answer, pydantic.ValidationError for an answer outside
    the schema and IncompleteAnswer for a cut one - all retried with the job (story 2 refines invalid answers)."""
    with engine.connect() as connection:
        article = extraction_repository.get_for_extraction(connection, article_id)
    if article is None or article.extracted or article.duplicate_of_id is not None or not article.plain_text:
        return None
    text, truncated = fit_text(article.plain_text, client.num_ctx)
    started = time.perf_counter()
    with limiter.slot():
        answer = client.chat_json(SYSTEM_PROMPT, user_message(article, text), event_schema.json_schema())
    duration_ms = round((time.perf_counter() - started) * 1000)
    if not answer.complete:
        raise IncompleteAnswer(f"Antwort nach {answer.answer_tokens} Tokens abgeschnitten")
    candidates, dropped = backed_candidates(event_schema.parse(answer.content), text)
    values = {
        "article_id": article_id,
        "model": answer.model,
        "schema_version": event_schema.SCHEMA_VERSION,
        "duration_ms": duration_ms,
        "input_chars": len(text),
        "truncated": truncated,
        "statements_dropped": dropped,
    }

    def store(connection: Connection) -> None:
        if extraction_repository.insert_extraction(connection, values, candidates) is not None:
            # IDs and counts only (datamodel.md, rule 13).
            fields = {
                "article_id": article_id, "candidates": len(candidates), "statements_dropped": dropped,
                "duration_ms": duration_ms, "prompt_tokens": answer.prompt_tokens, "answer_tokens": answer.answer_tokens,
            }
            logger.info("Events extracted", extra=fields)

    return store


# After the article: a model follows the last instruction it read most closely, and mirrors the article's language.
REMINDER = "Answer in English - titles, summaries, roles, descriptions, statements, time notes. Quotes verbatim."


def user_message(article, text: str) -> str:
    published = article.published_at.date().isoformat() if article.published_at else "unknown"
    header = f"Source: {article.source_name}\nPublished: {published}\nTitle: {article.title or 'unknown'}"
    return f"{header}\n\nArticle text:\n{text}\n\n{REMINDER}"


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
    for needle in (quote.strip(_QUOTE_MARKS), quote.strip(_QUOTE_ENDS)):
        start = plain.find(needle) if len(needle) >= MIN_QUOTE_CHARS else -1
        if start >= 0:
            return text[positions[start] : positions[start + len(needle) - 1] + 1]
    return None


def _plain(char: str) -> str:
    """A character as quotes are compared: plain marks, lower case where that keeps it one character."""
    char = char.translate(_PLAIN_MARKS)
    lower = char.lower()
    return lower if len(lower) == 1 else char


def backed_candidates(answer: event_schema.ExtractionAnswer, text: str) -> tuple[list[dict], int]:
    """The candidates as rows, each statement with the passage it quotes; statements without one dropped, and
    candidates left without a statement. Returns the rows and how many statements were dropped."""
    candidates, dropped = [], 0
    for event in answer.events:
        statements = []
        for statement in event.statements:
            quote = find_quote(statement.quote, text)
            if quote is None:
                dropped += 1
                continue
            statements.append({"kind": statement.kind, "text": statement.text, "quote": quote})
        if not statements:
            continue
        candidates.append({
            "event_type": event.event_type,
            "status": event.status,
            "title": event.title,
            "summary": event.summary,
            "country": event.location.country,
            "place": event.location.place,
            "time_note": event.time_note,
            "actors": [actor.model_dump(mode="json") for actor in event.actors],
            "impacts": [impact.model_dump(mode="json") for impact in event.impacts],
            "statements": statements,
        })
    return candidates, dropped


@dataclass(frozen=True)
class StatementView:
    kind: str
    text: str
    quote: str

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


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

    @property
    def type_label(self) -> str:
        return EVENT_TYPE_LABELS[self.event_type]

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def where(self) -> str:
        return ", ".join(part for part in (self.place, self.country) if part)

    @property
    def impact_lines(self) -> list[str]:
        return [
            f"{impact['target']} ({TARGET_KIND_LABELS[impact['target_kind']]}): {DIRECTION_LABELS[impact['direction']]}, "
            f"{HORIZON_LABELS[impact['horizon']]}" + (f" - {impact['description']}" if impact["description"] else "")
            for impact in self.impacts
        ]


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


def latest_for_article(engine: Engine, article_id: int) -> ExtractionView | None:
    """The article's latest extraction as its page shows it; None before the first."""
    with engine.connect() as connection:
        extraction = extraction_repository.latest(connection, article_id)
        if extraction is None:
            return None
        candidates = extraction_repository.candidates_of(connection, extraction.id)
        statements = extraction_repository.statements_of(connection, extraction.id)
    by_candidate: dict[int, list[StatementView]] = {}
    for row in statements:
        by_candidate.setdefault(row.candidate_id, []).append(StatementView(row.kind, row.text, row.quote))
    views = [
        CandidateView(
            id=row.id, event_type=row.event_type, status=row.status, title=row.title, summary=row.summary,
            country=row.country, place=row.place, time_note=row.time_note,
            actors=row.actors, impacts=row.impacts, statements=by_candidate.get(row.id, []),
        )
        for row in candidates
    ]
    return ExtractionView(
        model=extraction.model, schema_version=extraction.schema_version, extracted_at=extraction.extracted_at,
        duration_ms=extraction.duration_ms, input_chars=extraction.input_chars, truncated=extraction.truncated,
        statements_dropped=extraction.statements_dropped, candidates=views,
    )
