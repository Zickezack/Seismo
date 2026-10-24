"""The event schema the LLM extracts into (userstories.md, Strukturierte Ereignisextraktion, story 1): the JSON schema
Ollama constrains the answer to, and the check of the answer.

Versioned: a change of the fields, their meaning or the prompt (app/services/extraction_service.py) is a new
SCHEMA_VERSION, stored with every extraction (datamodel.md, rule 2). Every text the model writes is English; a quote
is verbatim from the article. What the article does not state stays empty - never a guess (rule 5).

Version 2 (story 2): place, time, actors and impacts carry a quote like every statement; a statement rates how faithful
it is to its quote (extraction), an event how sure its type, status and impacts are derived (inference).
"""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db.models import ConfidenceLevel, EventStatus, EventType, StatementKind

SCHEMA_VERSION = "2"
QUOTE_MAX = 500
MAX_EVENTS = 5
MAX_STATEMENTS = 5


class TargetKind(StrEnum):
    COMPANY = "company"
    SECTOR = "sector"
    COMMODITY = "commodity"
    COUNTRY = "country"
    MARKET = "market"
    OTHER = "other"


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNCLEAR = "unclear"


class Horizon(StrEnum):
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"
    YEARS = "years"
    UNKNOWN = "unknown"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Actor(_Strict):
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(max_length=200)
    quote: str = Field(min_length=1, max_length=QUOTE_MAX)


class Impact(_Strict):
    target: str = Field(min_length=1, max_length=200)
    target_kind: TargetKind
    direction: Direction
    horizon: Horizon
    description: str = Field(max_length=500)
    quote: str = Field(min_length=1, max_length=QUOTE_MAX)


# Required, though null: the answer names every detail, unknown as null, instead of leaving out a key - and with it
# the quote that backs it (a model left an optional quote out).
class Location(_Strict):
    country: str | None = Field(pattern="^[A-Z]{2}$")
    place: str | None = Field(max_length=200)
    quote: str | None = Field(max_length=QUOTE_MAX)


class ExtractedStatement(_Strict):
    kind: StatementKind
    text: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=QUOTE_MAX)
    confidence: ConfidenceLevel


class ExtractedEvent(_Strict):
    event_type: EventType
    status: EventStatus
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    confidence: ConfidenceLevel
    location: Location
    time_note: str | None = Field(max_length=200)
    time_quote: str | None = Field(max_length=QUOTE_MAX)
    actors: list[Actor] = Field(max_length=10)
    impacts: list[Impact] = Field(max_length=10)
    statements: list[ExtractedStatement] = Field(min_length=1, max_length=MAX_STATEMENTS)


class ExtractionAnswer(_Strict):
    events: list[ExtractedEvent] = Field(max_length=MAX_EVENTS)


def json_schema() -> dict:
    """The answer's JSON schema with every reference inlined: not every grammar converter follows $ref."""
    schema = ExtractionAnswer.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(value) for value in node]
        return node

    return inline(schema)


def parse(answer: str) -> ExtractionAnswer:
    """The model's answer, checked against the schema; raises pydantic.ValidationError."""
    return ExtractionAnswer.model_validate_json(answer)


def error_summary(error: ValidationError, limit: int = 10) -> str:
    """What is wrong with an answer, one line per error: where and what, never the value - it may quote the article
    (datamodel.md, rule 13)."""
    lines = [
        f"{'.'.join(str(part) for part in detail['loc']) or 'answer'}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)
    ]
    more = len(lines) - limit
    return "\n".join(lines[:limit] + ([f"and {more} more"] if more > 0 else []))
