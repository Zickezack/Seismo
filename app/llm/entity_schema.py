"""The answer of the entity recognition (userstories.md, Entitäten und Wissensmodell, story 1): for each name the reports
of an extraction give, what it stands for - the JSON schema Ollama constrains the answer to, and its check.

Versioned like the event schema: a change of the fields, their meaning or the prompt (app/services/entity_service.py)
is a new SCHEMA_VERSION, stored with every recognition (datamodel.md, rule 2).

Version 2 (story 2): the relationships the reports' statements state between the names, each citing its statement.
"""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.db.models import AliasScheme, EntityKind, RelationshipKind

SCHEMA_VERSION = "2"
MAX_NAMES = 50
MAX_IDENTIFIERS = 3
MAX_RELATIONSHIPS = 20

# What a name stands for: one of the kinds of entity, a private individual, or none - the two make no entity.
RecognisedKind = StrEnum(
    "RecognisedKind", {kind.name: kind.value for kind in EntityKind} | {"PRIVATE_PERSON": "private_person", "NONE": "none"}
)


class IdentifierScheme(StrEnum):
    """The identifiers a report may state; a country's code and a starter list's key have fields of their own."""

    ISIN = AliasScheme.ISIN.value
    LEI = AliasScheme.LEI.value
    TICKER = AliasScheme.TICKER.value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatedIdentifier(_Strict):
    scheme: IdentifierScheme
    value: str = Field(min_length=1, max_length=40)


# Every key required, though null: the answer names each detail, unknown as null (as event_schema.Location).
class RecognisedName(_Strict):
    mention: int  # the name's number in the question
    # Of a named individual the public or business office the reports state, before the kind: a person without one
    # is no entity (datamodel.md, rule 13).
    public_role: str | None = Field(max_length=200)
    kind: RecognisedKind
    name: str | None = Field(max_length=200)
    short_name: str | None = Field(max_length=100)
    same_as: int | None  # the number of a known entity
    parent: str | None = Field(max_length=200)
    country_code: str | None = Field(pattern="^[A-Z]{2}$")
    category: str | None = Field(max_length=60)  # the key of a listed sector or commodity
    identifiers: list[StatedIdentifier] = Field(max_length=MAX_IDENTIFIERS)


class StatedRelationship(_Strict):
    subject: int  # a name's number
    kind: RelationshipKind
    object: int
    ended: bool
    statement: int  # the number of the statement that states it


class RecognitionAnswer(_Strict):
    entities: list[RecognisedName] = Field(max_length=MAX_NAMES)
    relationships: list[StatedRelationship] = Field(max_length=MAX_RELATIONSHIPS)


def json_schema(names: int, known: int, categories: list[str], statements: int = 0) -> dict:
    """The answer's JSON schema for a question with this many names, known entities and statements and these listed
    keys: a number or key outside them cannot be written. Without a statement or a second name, no relationship."""
    schema = RecognitionAnswer.model_json_schema()
    definitions = schema.pop("$defs")
    item = definitions["RecognisedName"]
    item["properties"]["mention"] = {"enum": list(range(1, names + 1))}
    item["properties"]["same_as"] = {"enum": [*range(1, known + 1), None]}
    item["properties"]["category"] = {"enum": [*categories, None]}
    item["properties"]["kind"] = {"enum": [kind.value for kind in RecognisedKind]}
    item["properties"]["identifiers"]["items"] = definitions["StatedIdentifier"]
    definitions["StatedIdentifier"]["properties"]["scheme"] = {"enum": [scheme.value for scheme in IdentifierScheme]}
    schema["properties"]["entities"]["items"] = item
    relationship = definitions["StatedRelationship"]
    # Never an empty enum, which a grammar converter may refuse; maxItems 0 keeps it unused then.
    relationship["properties"]["subject"] = relationship["properties"]["object"] = {"enum": list(range(1, max(names, 1) + 1))}
    relationship["properties"]["statement"] = {"enum": list(range(1, max(statements, 1) + 1))}
    relationship["properties"]["kind"] = {"enum": [kind.value for kind in RelationshipKind]}
    relationships = schema["properties"]["relationships"]
    relationships["items"] = relationship
    if not statements or names < 2:
        relationships["maxItems"] = 0
    return schema


def parse(answer: str) -> RecognitionAnswer:
    """The model's answer, checked against the schema; raises pydantic.ValidationError."""
    return RecognitionAnswer.model_validate_json(answer)


def error_summary(error: ValidationError, limit: int = 5) -> str:
    """Where and what, never the value: it may name a person (datamodel.md, rule 13)."""
    lines = [
        f"{'.'.join(str(part) for part in detail['loc']) or 'answer'}: {detail['msg']}"
        for detail in error.errors(include_input=False, include_url=False)
    ]
    return "; ".join(lines[:limit])
