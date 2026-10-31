"""Entity recognition (userstories.md, Entitäten und Wissensmodell, story 1): the companies, subsidiaries, persons,
countries, authorities, organisations, products, commodities and sectors the reports name, each name form and
identifier resolved to one entity, an uncertain match flagged (datamodel.md, rule 9).

One LLM call per extraction (job "Entitäten erkennen"): the model is shown the distinct names its reports give - the
actors and the targets of impacts, each with its role and quote - and, for each, the known entities with a similar
name form (pg_trgm, at most CANDIDATES_PER_NAME). It answers what each name stands for (a kind of entity, or none), its
usual English name, a short form the report uses, the known entity it is when it is sure, a subsidiary's parent or a
product's maker, a country's code, a listed sector's or commodity's key, and identifiers its quote states.

Resolved in the transaction that completes the job, under an advisory lock, in this order: an identifier an entity
has (a country's code, a listed item's key, an ISIN, LEI or ticker) and a name form of an entity of the kind are
certain; the known entity the LLM judged it - a similar name only - is uncertain, flagged until the analyst confirms
it (the admin's decision of 2026-10-31); else it founds a new entity. A name form recognised together with an
uncertain one is uncertain as well. Sectors and commodities come from the starter list only. Persons only in a public
or business role: the model names a named individual's position first, and one without - or answered as a private
individual - is no entity (datamodel.md, rule 13).

Should an entity with a similar name have come since the LLM was asked, the effect raises JobDeferred and the job
asks anew: two recognitions never found two entities for one name the LLM would have judged the same.

The same call names the relationships the reports' statements state between the names (story 2, schema 2), each
citing its statement; one between two names that became entities of fitting kinds is an Angabe of the relationship
(relationship_service), its evidence the statement's Beleg.
"""
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import Connection, Engine

from app.db.models import AliasMatch, AliasScheme, EntityKind, MentionSource
from app.jobs.errors import JobDeferred
from app.llm import entity_schema
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import entity_repository, extraction_repository, relationship_repository
from app.services import job_service, relationship_service, seed_service
from app.services.entity_names import country_code, identifier_key, name_key, stated_in
from app.services.extraction_service import DIRECTION_LABELS, HORIZON_LABELS

logger = logging.getLogger(__name__)

# Queued per sweep: a recognition takes a few seconds.
QUEUE_LIMIT = 100
# Known entities offered per name, and at most in one question.
CANDIDATES_PER_NAME = 3
MAX_KNOWN = 40
LIST_LIMIT = 200
MENTION_LIMIT = 100
# Names a form offers to choose an entity from; beyond them it takes a number or a name typed in.
CHOICES_LIMIT = 2000
# Other name forms the LLM is shown of a known entity.
FORMS_SHOWN = 3

# Kinds a name form is matched across: the model calls one company a subsidiary in one report and a company in the
# next.
COMPANY_KINDS = (EntityKind.COMPANY, EntityKind.SUBSIDIARY)
CATALOGUE_KINDS = (EntityKind.SECTOR, EntityKind.COMMODITY)
PARENT_KINDS = (EntityKind.SUBSIDIARY, EntityKind.PRODUCT)
# Answers that make no entity. A private individual has a kind of its own in the answer, and a person needs the office
# the model names first: told only to answer none for one, the model named 13 of 13 victims, suspects and relatives of
# the dev data a person (2026-10-31) - datamodel.md, rule 13.
NO_ENTITY = ("none", "private_person")

SYSTEM_PROMPT = """You recognise the entities that news reports name, and the relationships between them, for an entity
database. Answer only with JSON in the given schema: in entities one item for each numbered name.

- public_role - first, for a named individual: the public or business position the reports give them, now or
  formerly, in a few words, e.g. "German Chancellor", "Mayor of Cali", "CEO of Cisco", "US First Lady", "State
  Department spokesperson", "national team player". Every head of state or government, minister, member of
  parliament, judge, official, diplomat, party or group leader, executive, athlete or artist has one. null for anyone
  else, and for an individual named only as concerned: a victim, a suspect, a witness, a relative, a resident, a
  voter, an employee, a local business owner.
- kind - what the name stands for:
  company: a business of any kind - also a software or security vendor, a bank, a sports club, a media outlet.
  subsidiary: a company owned by another company.
  person: a named individual with a public_role.
  private_person: a named individual without one.
  country: a sovereign state or a territory with an ISO country code.
  authority: a named body of one state - a government, ministry, agency, central bank, regulator, court, police
  force, the armed forces.
  organization: only an international organisation (UN, OPEC, EU, IMF), a party, a union, an NGO, a movement or an
  armed group - never a business.
  product: a named product, brand or service of a company.
  commodity, sector: only one of the listed ones, its key as category.
  none: anything else - a group of people (residents, rescuers, protesters, voters), a body named only generically
  (local authorities, emergency services, the government without its country), a place that is no country (a city,
  a region, a building, a plant), infrastructure without a name of its own (the power grid), a market, an index, a
  relationship, an abstract thing, a commodity or sector that fits none of the listed ones.
- name: its usual full English name, without legal form and title: "Volodymyr Zelenskyy" for "President Vladimir
  Zelensky", "Toyota Motor" for "Toyota Motor Corp.", "European Central Bank" for "EZB". null for none.
- short_name: an abbreviation or short form the report itself uses for it, e.g. "ECB"; else null.
- same_as: the number of the known entity it is - only when you are sure it is the same one, not merely a similar
  name: the Central Bank of Chile is not the Central Bank of Brazil. Else null.
- parent: of a subsidiary the company that owns it, of a product the company that makes it, by its usual English
  name - only when you know it for certain; else null.
- country_code: of a country its ISO 3166-1 alpha-2 code; else null.
- category: of a commodity or a sector the key of the listed one; else null.
- identifiers: an ISIN, an LEI or a stock ticker as EXCHANGE:SYMBOL (e.g. "NASDAQ:AAPL") only when the name's quote
  states it; never one you know but the quote does not state.
- relationships: only those a numbered statement states between two numbered names that are entities, each once:
  supplies - subject sells or delivers goods or services to object: its supplier, object its customer - the one that
  delivers is the subject, never the one that uses what it gets;
  competes - the statement says the two compete or are rivals; partners do not compete;
  regulates - subject, an authority or organisation, oversees, licenses, fines or sanctions object;
  depends_on - subject depends on object, a listed commodity.
  ended: true when the statement says it ended - a contract cancelled, a supplier dropped, a dependency given up.
  statement: the number of the statement that states it. None you know but the statements do not state; none for
  an event between the two that is no lasting relationship (an attack, a meeting, a single sale)."""

KIND_LABELS = {
    EntityKind.COMPANY: "Unternehmen",
    EntityKind.SUBSIDIARY: "Tochterunternehmen",
    EntityKind.PERSON: "Person",
    EntityKind.COUNTRY: "Land",
    EntityKind.AUTHORITY: "Behörde",
    EntityKind.ORGANIZATION: "Organisation",
    EntityKind.PRODUCT: "Produkt",
    EntityKind.COMMODITY: "Rohstoff",
    EntityKind.SECTOR: "Branche",
}
SCHEME_LABELS = {
    AliasScheme.NAME: "Name",
    AliasScheme.ISO3166: "ISO-Ländercode",
    AliasScheme.ISIN: "ISIN",
    AliasScheme.LEI: "LEI",
    AliasScheme.TICKER: "Ticker",
    AliasScheme.CATALOGUE: "Starterliste",
}
MATCH_LABELS = {
    AliasMatch.FOUNDING: "Erste Nennung",
    AliasMatch.NAME: "Mit bekanntem Namen erkannt",
    AliasMatch.IDENTIFIER: "Mit bekannter Kennung erkannt",
    AliasMatch.LLM: "Ähnlicher Name, vom LLM als gleich beurteilt",
    AliasMatch.SEED: "Starterliste",
    AliasMatch.SPLIT: "Abgetrennt",
}
PARENT_LABELS = {EntityKind.SUBSIDIARY: "Mutterunternehmen", EntityKind.PRODUCT: "Hersteller"}


class UnusableAnswer(Exception):
    """The model's answer was cut or outside the schema: retried with the job."""


def matched_kinds(kind: str) -> tuple[str, ...]:
    """The kinds whose name forms a name of this kind is matched with."""
    return COMPANY_KINDS if kind in COMPANY_KINDS else (kind,)


@dataclass(frozen=True)
class Place:
    """Where a report gives a name: one of its actors or impacts."""

    candidate_id: int
    source: str
    position: int


@dataclass
class Name:
    """A name the reports of an extraction give, once per key, with every place they give it."""

    text: str  # as the extraction wrote it the first time
    key: str
    context: str  # what the first report says of it
    quotes: list[str] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)


@dataclass(frozen=True)
class Known:
    """A known entity the LLM is shown, numbered in the question."""

    id: int
    kind: str
    name: str
    forms: list[str]


@dataclass(frozen=True)
class Recognised:
    """What a name stands for, as the model answered and the checks left it."""

    name: Name
    kind: str
    english: str  # the name of an entity it founds
    forms: list[str]  # its name forms, each key once: as the report gives it, the usual English, the short one
    identifiers: list[tuple[str, str]]  # (scheme, key), each checked
    same_as: int | None = None  # the id of the known entity the LLM judged it
    parent: str | None = None


def collect_names(candidates: Iterable) -> list[Name]:
    """The distinct names of the reports' actors and impact targets, in the order they come, at most MAX_NAMES; one
    without a letter or digit is none."""
    names: dict[str, Name] = {}
    for candidate in candidates:
        given = [(MentionSource.ACTOR, position, actor["name"], f"actor, role: {actor['role'] or 'unknown'}", actor.get("quote"))
                 for position, actor in enumerate(candidate.actors)]
        given += [(MentionSource.IMPACT, position, impact["target"], _impact_context(impact), impact.get("quote"))
                  for position, impact in enumerate(candidate.impacts)]
        for source, position, text, context, quote in given:
            key = name_key(text)
            if not key or (key not in names and len(names) >= entity_schema.MAX_NAMES):
                continue
            name = names.setdefault(key, Name(text.strip(), key, context))
            name.places.append(Place(candidate.id, source, position))
            if quote and quote not in name.quotes:
                name.quotes.append(quote)
    return list(names.values())


def _impact_context(impact: dict) -> str:
    context = f"affected by an impact, {impact['direction']}, horizon {impact['horizon']}"
    return f"{context}: {impact['description']}" if impact.get("description") else context


@dataclass(frozen=True)
class Category:
    key: str
    kind: str
    english: str


def messages(
    candidates: Iterable, names: list[Name], known: list[Known], categories: list[Category], statements: Iterable = ()
) -> list[dict]:
    """The reports, their names, their statements and the known entities, and the listed sectors and commodities, for
    the LLM."""
    reports = "\n".join(f"- {candidate.title}: {candidate.summary}" for candidate in candidates)
    stated = "\n".join(
        f'{number}. {statement.text} Quote: "{statement.quote}"' for number, statement in enumerate(statements, start=1)
    ) or "none"
    numbered = "\n".join(
        f'{number}. "{name.text}" - {name.context}' + (f'. Quote: "{name.quotes[0]}"' if name.quotes else "")
        for number, name in enumerate(names, start=1)
    )
    known_lines = "\n".join(
        f"{number}. {entity.name} ({entity.kind}" + (f"; also: {', '.join(entity.forms)}" if entity.forms else "") + ")"
        for number, entity in enumerate(known, start=1)
    ) or "none"
    listed = []
    for kind, title in ((EntityKind.SECTOR, "Listed sectors"), (EntityKind.COMMODITY, "Listed commodities")):
        lines = [f"{category.key}: {category.english}" for category in categories if category.kind == kind]
        listed.append(f"{title} (category key: what it covers):\n" + ("\n".join(lines) or "none - use none for any"))
    user = (
        f"Reports:\n{reports}\n\nNames:\n{numbered}\n\nStatements:\n{stated}\n\nKnown entities:\n{known_lines}\n\n"
        + "\n\n".join(listed)
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def recognised(answer: entity_schema.RecognitionAnswer, names: list[Name], known: list[Known], catalogue: Iterable[str]) -> list[Recognised]:
    """What the answer makes of the names, after the checks: a name answered twice counts the first time; none, a
    sector or commodity without a listed key, and a name without one stay without an entity; a country's code must be
    one; an identifier must be valid and stated in the name's quote."""
    catalogue = set(catalogue)
    results, seen = [], set()
    for item in answer.entities:
        index = item.mention - 1
        if not 0 <= index < len(names) or index in seen or item.kind in NO_ENTITY:
            continue
        if item.kind == EntityKind.PERSON and not (item.public_role or "").strip():
            continue
        seen.add(index)
        name = names[index]
        if item.kind in CATALOGUE_KINDS:
            if item.category in catalogue:
                results.append(Recognised(name, item.kind, name.text, [name.text], [(AliasScheme.CATALOGUE, item.category)]))
            continue
        english = " ".join((item.name or "").split())
        english = english if name_key(english) else name.text
        forms = _distinct_forms([name.text, english, item.short_name or ""])
        identifiers = []
        if item.kind == EntityKind.COUNTRY and (code := country_code(item.country_code)):
            identifiers.append((AliasScheme.ISO3166, code))
        for stated in item.identifiers:
            key = identifier_key(stated.scheme, stated.value)
            if key and any(stated_in(stated.scheme, key, quote) for quote in name.quotes) and (stated.scheme, key) not in identifiers:
                identifiers.append((stated.scheme.value, key))
        same_as = known[item.same_as - 1].id if item.same_as and 1 <= item.same_as <= len(known) else None
        parent = " ".join((item.parent or "").split()) if item.kind in PARENT_KINDS else ""
        parent = parent if name_key(parent) and name_key(parent) not in {name_key(form) for form in forms} else None
        results.append(Recognised(name, item.kind, english, forms, identifiers, same_as, parent))
    return results


@dataclass(frozen=True)
class Stated:
    """A relationship a statement states between two names, as the answer gives it."""

    subject: Name
    kind: str
    object: Name
    ended: bool
    statement: object  # the statement's row: id, candidate, text, quote, its report's time


def stated_relationships(answer: entity_schema.RecognitionAnswer, names: list[Name], statements: list) -> list[Stated]:
    """The relationships of the answer between two different names, each citing a statement of the question; the same
    one again from the same statement counts once."""
    results, seen = [], set()
    for relationship in answer.relationships:
        subject, object_, statement = relationship.subject - 1, relationship.object - 1, relationship.statement - 1
        key = (subject, relationship.kind, object_, statement)
        if subject == object_ or key in seen or not (0 <= subject < len(names) and 0 <= object_ < len(names) and 0 <= statement < len(statements)):
            continue
        seen.add(key)
        results.append(Stated(names[subject], relationship.kind, names[object_], relationship.ended, statements[statement]))
    return results


def _distinct_forms(forms: list[str]) -> list[str]:
    distinct: dict[str, str] = {}
    for form in forms:
        form = " ".join(form.split())
        if (key := name_key(form)) and key not in distinct:
            distinct[key] = form
    return list(distinct.values())


def enqueue_due(engine: Engine) -> list[int]:
    """Queue a recognition for the extractions with reports and without one, the earliest article first. The worker
    calls it only while Ollama serves the model."""
    kind = job_service.RECOGNISE_ENTITIES_JOB_KIND
    with engine.begin() as connection:
        due = entity_repository.due(connection, kind, QUEUE_LIMIT)
        job_ids = [
            job_service.enqueue(connection, kind, {"extraction_id": extraction_id}, dedupe_key=f"{kind}:{extraction_id}")
            for extraction_id in due
        ]
    return [job_id for job_id in job_ids if job_id is not None]


def recognise(
    engine: Engine,
    extraction_id: int,
    client: OllamaClient,
    limiter: LlmCallLimiter,
    rerun: bool = False,
    reprocessing_id: int | None = None,
) -> Callable[[Connection], None] | None:
    """First phase of a recognition job: ask the LLM what the names of the extraction's reports stand for. Returns the
    effect that resolves them, or None when the extraction is gone, replaced or recognised already. `rerun` (job
    "Entitäten neu erkennen", reprocessing_service): in place of its recognition - None without one; the effect
    replaces the mentions, and the Angaben of the run before no longer count. LlmError (app/llm/ollama.py) or
    UnusableAnswer when the LLM brought no usable answer: retried with the job."""
    with engine.connect() as connection:
        before = entity_repository.reading(connection, extraction_id)
        if before is None or (before.recognised_at is not None) != rerun:
            return None
        candidates = extraction_repository.candidates_of(connection, extraction_id)
        if not candidates:
            return None
        names = collect_names(candidates)
        offered = {name.key: [entity_id for entity_id, _ in entity_repository.similar(connection, name.key, CANDIDATES_PER_NAME)] for name in names}
        known = _known(connection, offered)
        categories = _categories(entity_repository.catalogue(connection))
        statements = list(relationship_repository.statements_of(connection, extraction_id))
    run = {
        "extraction_id": extraction_id, "schema_version": entity_schema.SCHEMA_VERSION, "names": len(names), "model": None,
        "duration_ms": None, "reprocessing_id": reprocessing_id,
    }
    items, stated, started = [], [], time.perf_counter()
    if names:
        schema = entity_schema.json_schema(len(names), len(known), [category.key for category in categories], len(statements))
        with limiter.slot():
            answer = client.chat_json(messages(candidates, names, known, categories, statements), schema)
        if not answer.complete:
            raise UnusableAnswer(f"Antwort abgeschnitten nach {answer.answer_tokens} Tokens")
        try:
            parsed = entity_schema.parse(answer.content)
        except ValidationError as error:
            raise UnusableAnswer(f"Antwort ausserhalb des Schemas: {entity_schema.error_summary(error)}") from None
        items = recognised(parsed, names, known, [category.key for category in categories])
        stated = stated_relationships(parsed, names, statements)
        run |= {"model": answer.model, "duration_ms": round((time.perf_counter() - started) * 1000)}

    def store(connection: Connection) -> None:
        entity_repository.lock_entities(connection)
        now = entity_repository.reading(connection, extraction_id)
        if now is None or now.recognised_at != before.recognised_at:
            # Replaced, or recognised (again) meanwhile.
            return
        if rerun:
            entity_repository.delete_mentions(connection, extraction_id)
            relationship_repository.mark_replaced(connection, extraction_id)
        created: set[int] = set()
        resolved: dict[str, _Alias] = {}
        mentions, uncertain = [], 0
        for item in items:
            alias = _resolve(connection, item, offered[item.name.key], created)
            if alias is None:
                continue
            resolved[item.name.key] = alias
            uncertain += alias.uncertain
            mentions += [
                {"candidate_id": place.candidate_id, "alias_id": alias.id, "source": place.source, "position": place.position}
                for place in item.name.places
            ]
        entity_repository.insert_mentions(connection, mentions)
        relationships = _record_relationships(connection, stated, resolved)
        values = run | {"linked": len(resolved), "relationships": relationships}
        if rerun:
            entity_repository.replace_recognition(connection, values)
        else:
            entity_repository.insert_recognition(connection, values)
        # IDs and counts only, never a name (datamodel.md, rule 13). Not "created": a LogRecord has that already.
        fields = {
            "extraction_id": extraction_id, "names": len(names), "linked": len(resolved), "founded": len(created),
            "uncertain": uncertain, "relationships": relationships, "rerun": rerun, "reprocessing_id": reprocessing_id,
        }
        logger.info("Entities recognised", extra=fields | {"duration_ms": run.get("duration_ms")})

    return store


def _record_relationships(connection: Connection, stated: list[Stated], resolved: dict[str, "_Alias"]) -> int:
    """The Angaben of the stated relationships whose two names became entities of kinds that can stand in them; their
    number."""
    pairs = [(each, resolved.get(each.subject.key), resolved.get(each.object.key)) for each in stated]
    pairs = [(each, subject, object_) for each, subject, object_ in pairs if subject and object_ and subject.entity_id != object_.entity_id]
    kinds = {row.id: row.kind for row in entity_repository.entities(connection, {alias.entity_id for _, s, o in pairs for alias in (s, o)}).values()}
    recorded = 0
    for each, subject, object_ in pairs:
        if not relationship_service.allowed(each.kind, kinds[subject.entity_id], kinds[object_.entity_id]):
            continue
        relationship_service.record_reported(
            connection, each.kind, (subject.entity_id, subject.id), (object_.entity_id, object_.id), each.statement.candidate_id,
            each.statement.id, each.statement.moment.astimezone(UTC).date(), each.ended,
        )
        recorded += 1
    return recorded


def _known(connection: Connection, offered: dict[str, list[int]]) -> list[Known]:
    ids = list(dict.fromkeys(entity_id for found in offered.values() for entity_id in found))[:MAX_KNOWN]
    rows = entity_repository.entities(connection, ids)
    forms = entity_repository.name_forms(connection, ids)
    return [
        Known(entity_id, rows[entity_id].kind, rows[entity_id].name,
              [form for form in forms.get(entity_id, []) if form != rows[entity_id].name][:FORMS_SHOWN])
        for entity_id in ids if entity_id in rows
    ]


def _categories(catalogue: dict[str, int]) -> list[Category]:
    """The listed sectors and commodities imported, each with what it covers in English for the LLM."""
    items = {item["key"]: item for item in seed_service.entity_catalogue()}
    return [Category(key, items[key]["kind"], items[key]["english"]) for key in sorted(catalogue) if key in items]


@dataclass
class _Alias:
    id: int
    uncertain: bool
    entity_id: int


def _resolve(connection: Connection, item: Recognised, offered: list[int], created: set[int]) -> _Alias | None:
    """The entity the name stands for - known by an identifier or a name form, judged by the LLM, or new - with every
    name form and identifier of the item it lacks; the alias of the name as the report gives it. None for a listed
    item that is no longer there."""
    entity_id, uncertain, match = None, False, AliasMatch.FOUNDING
    for scheme, key in item.identifiers:
        if found := entity_repository.find_identifier(connection, scheme, key):
            entity_id, uncertain, match = found.entity_id, found.uncertain, AliasMatch.IDENTIFIER
            break
    if entity_id is None and item.kind in CATALOGUE_KINDS:
        return None
    if entity_id is None:
        keys = [name_key(form) for form in item.forms]
        found = {}
        for row in entity_repository.find_names(connection, keys, matched_kinds(item.kind)):
            found.setdefault(row.key, row)
        # The usual English name first, then the report's own form, then the short one.
        for key in [name_key(item.english), *keys]:
            if key in found:
                entity_id, uncertain, match = found[key].entity_id, found[key].uncertain, AliasMatch.NAME
                break
    if entity_id is None:
        now = {entity_id for entity_id, _ in entity_repository.similar(connection, item.name.key, CANDIDATES_PER_NAME)} - created
        if not now <= set(offered):
            # An entity with a similar name came since the LLM was asked: it decides anew.
            raise JobDeferred(datetime.now(UTC))
        if item.same_as is not None and (target := _live(connection, item.same_as)) is not None:
            entity_id, uncertain, match = target, True, AliasMatch.LLM
    if entity_id is None:
        entity_id = entity_repository.create_entity(connection, {"kind": item.kind, "name": item.english})
        created.add(entity_id)
    aliases = {key: _Alias(row.id, row.uncertain, entity_id) for key, row in entity_repository.alias_keys(connection, entity_id).items()}
    for scheme, value, key in [(AliasScheme.NAME, form, name_key(form)) for form in item.forms] + [(s, k, k) for s, k in item.identifiers]:
        if (scheme, key) in aliases or (scheme != AliasScheme.NAME and entity_repository.identifier_taken(connection, scheme, key)):
            continue
        similarity = entity_repository.best_similarity(connection, entity_id, key) if uncertain and scheme == AliasScheme.NAME else None
        values = {"entity_id": entity_id, "scheme": scheme, "value": value, "key": key, "match": match, "uncertain": uncertain,
                  "similarity": None if similarity is None else round(similarity, 3)}
        aliases[(scheme, key)] = _Alias(entity_repository.insert_alias(connection, values), uncertain, entity_id)
    if item.parent:
        _set_parent(connection, entity_id, item.parent, created)
    return aliases[(AliasScheme.NAME, item.name.key)]


def _set_parent(connection: Connection, entity_id: int, parent: str, created: set[int]) -> None:
    """The company that owns a subsidiary or makes a product: known by its name, else founded with it."""
    key = name_key(parent)
    rows = entity_repository.find_names(connection, [key], COMPANY_KINDS)
    if rows:
        parent_id = rows[0].entity_id
    else:
        parent_id = entity_repository.create_entity(connection, {"kind": EntityKind.COMPANY, "name": parent})
        created.add(parent_id)
        entity_repository.insert_alias(connection, {
            "entity_id": parent_id, "scheme": AliasScheme.NAME, "value": parent, "key": key, "match": AliasMatch.FOUNDING,
        })
    if parent_id != entity_id:
        entity_repository.set_parent(connection, entity_id, parent_id)


def _live(connection: Connection, entity_id: int) -> int | None:
    """The entity, or the one it was merged into; None when it is gone."""
    seen = set()
    while entity_id not in seen:
        seen.add(entity_id)
        row = entity_repository.get(connection, entity_id)
        if row is None:
            return None
        if row.merged_into_id is None:
            return row.id
        entity_id = row.merged_into_id
    return None


@dataclass(frozen=True)
class EntityRef:
    id: int
    name: str
    kind: str | None = None

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind] if self.kind else ""


@dataclass(frozen=True)
class EntityListItem:
    id: int
    kind: str
    name: str
    alias_count: int
    uncertain_count: int  # name forms not confirmed yet
    report_count: int
    event_count: int

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


@dataclass(frozen=True)
class EntityOverview:
    entities: list[EntityListItem]  # at most LIST_LIMIT, by name
    total: int  # matching the filters
    waiting: int  # extractions whose entities are not recognised yet


@dataclass(frozen=True)
class AliasView:
    """A name form or identifier with how it came to the entity."""

    id: int
    scheme: str
    value: str
    match: str
    similarity: float | None
    uncertain: bool
    confirmed_at: datetime | None
    created_at: datetime
    mention_count: int

    @property
    def scheme_label(self) -> str:
        return SCHEME_LABELS[self.scheme]

    @property
    def match_label(self) -> str:
        label = MATCH_LABELS[self.match]
        if self.similarity is not None:
            return f"{label} (Ähnlichkeit {f'{self.similarity:.2f}'.replace('.', ',')})"
        return label

    @property
    def is_name(self) -> bool:
        return self.scheme == AliasScheme.NAME


@dataclass(frozen=True)
class MentionView:
    """A report that names the entity, with what it says of it."""

    id: int
    source: str
    detail: dict | None  # the actor or impact as extracted
    alias_value: str
    uncertain: bool
    candidate_id: int
    report_title: str
    event_id: int | None
    event_title: str | None
    article_id: int
    moment: datetime
    source_display_name: str

    @property
    def role_label(self) -> str:
        """As actor its role; as an impact's target the impact's direction and horizon, and what it describes."""
        detail = self.detail or {}
        if self.source == MentionSource.ACTOR:
            return f"Akteur: {detail.get('role')}" if detail.get("role") else "Akteur"
        label = f"Betroffen: {DIRECTION_LABELS.get(detail.get('direction'), 'unklar')}, {HORIZON_LABELS.get(detail.get('horizon'), '')}"
        return f"{label} - {detail['description']}" if detail.get("description") else label

    @property
    def quote(self) -> str | None:
        return (self.detail or {}).get("quote")


@dataclass(frozen=True)
class EntityDetail:
    entity: EntityListItem
    aliases: list[AliasView]
    mentions: list[MentionView]  # the latest MENTION_LIMIT
    mention_total: int
    parent: EntityRef | None = None
    children: list[EntityRef] = field(default_factory=list)
    merged_into: EntityRef | None = None

    @property
    def parent_label(self) -> str:
        return PARENT_LABELS.get(self.entity.kind, "Gehört zu")

    @property
    def uncertain(self) -> list[AliasView]:
        return [alias for alias in self.aliases if alias.uncertain]


@dataclass(frozen=True)
class EventEntity:
    """An entity the reports of an event name."""

    id: int
    kind: str
    name: str
    report_count: int
    uncertain: bool  # named by an uncertain name form
    forms: list[str]

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


def overview(engine: Engine, kind: str | None = None, uncertain_only: bool = False, search: str | None = None) -> EntityOverview:
    """The entities, by name; an unknown kind filters nothing."""
    kind = kind if kind in KIND_LABELS else None
    search = (search or "").strip() or None
    with engine.connect() as connection:
        rows = entity_repository.listing(connection, kind, uncertain_only, search, LIST_LIMIT)
        total = entity_repository.count_listing(connection, kind, uncertain_only, search)
        waiting = entity_repository.count_unrecognised(connection)
    return EntityOverview([_list_item(row) for row in rows], total, waiting)


def listed(engine: Engine, entity_ids: Iterable[int]) -> list[EntityListItem]:
    """These entities as the list shows them, merged ones left out."""
    with engine.connect() as connection:
        return [_list_item(row) for row in entity_repository.listed(connection, entity_ids)]


def detail(engine: Engine, entity_id: int) -> EntityDetail | None:
    with engine.connect() as connection:
        row = entity_repository.get(connection, entity_id)
        if row is None:
            return None
        counts = entity_repository.listed(connection, [entity_id])
        aliases = [_view(AliasView, each) for each in entity_repository.aliases(connection, entity_id)]
        mentions = [_view(MentionView, each) for each in entity_repository.mentions(connection, entity_id, MENTION_LIMIT)]
        total = entity_repository.count_mentions(connection, entity_id)
        children = [EntityRef(each.id, each.name, each.kind) for each in entity_repository.children(connection, entity_id)]
        related = entity_repository.entities(connection, [each for each in (row.parent_id, row.merged_into_id) if each is not None])
    # A merged entity is listed no more: its own row, without name forms and reports.
    item = _list_item(counts[0]) if counts else EntityListItem(row.id, row.kind, row.name, 0, 0, 0, 0)
    parent = related.get(row.parent_id)
    merged_into = related.get(row.merged_into_id)
    return EntityDetail(
        item, aliases, mentions, total, children=children,
        parent=EntityRef(parent.id, parent.name, parent.kind) if parent else None,
        merged_into=EntityRef(merged_into.id, merged_into.name, merged_into.kind) if merged_into else None,
    )


def of_event(engine: Engine, event_id: int) -> list[EventEntity]:
    with engine.connect() as connection:
        rows = entity_repository.of_event(connection, event_id)
    return [EventEntity(row.id, row.kind, row.name, row.report_count, row.uncertain, sorted(row.forms)) for row in rows]


def _list_item(row) -> EntityListItem:
    return _view(EntityListItem, row)


def _view(view, row):
    names = {each.name for each in fields(view)}
    return view(**{name: value for name, value in row._mapping.items() if name in names})


def choices(engine: Engine) -> list[EntityRef]:
    """The entities a form offers to choose from, by name, at most CHOICES_LIMIT."""
    with engine.connect() as connection:
        return [EntityRef(row.id, row.name) for row in entity_repository.choices(connection, CHOICES_LIMIT)]
