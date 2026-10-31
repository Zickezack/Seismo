"""Relationships between entities (userstories.md, Entitäten und Wissensmodell, story 2): who supplies whom, who
competes with whom, who regulates whom, who depends on which commodity - each with its sources, a validity period and
a quality flag, the analyst's review (datamodel.md, rule 10).

A relationship is only its two entities and its kind; everything else are Angaben, appended and never changed: a
report states that it holds or that it ended, backed by the quote of one of its statements (entity_service, schema 2),
or the analyst sets its validity and review, naming a source. Its state follows from the Angaben in the order Seismo
learned them (replay), so every earlier state stays to be seen and the state at any past moment can be rebuilt
(datamodel.md, rules 3 and 11). Reports only date it: one that it holds opens it from its day or moves its start
earlier, one that it ended closes it; reports never change the review. The analyst's Angabe sets dates and review as
given (the admin's decision of 2026-10-31).

A supplier and its customer are one relationship, seen from either side; a competition is one relationship whose
subject is the entity with the lower id. Every write runs under the entities' lock (entity_repository.lock_entities):
a merge moves the Angaben of the merged entity's relationships to those of the entity it went into, a split moves the
reports' Angaben that name the entity by the name forms split off.
"""
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import Connection, Engine

from app.db.models import EntityKind, RecordOrigin, RelationshipKind, ReviewStatus
from app.repositories import entity_repository, relationship_repository
from app.services import entity_service  # a module import: entity_service imports this one as well
from app.services.bulk import BulkResult
from app.services.extraction_service import level_label

logger = logging.getLogger(__name__)

MAX_NOTE_CHARS = 1000
COMPANIES = (EntityKind.COMPANY, EntityKind.SUBSIDIARY)
# The kinds of entity each side of a relationship may be: a country supplies gas, a sector depends on a commodity,
# an organisation sanctions a company.
SIDES = {
    RelationshipKind.SUPPLIES: (
        (*COMPANIES, EntityKind.COUNTRY),
        (*COMPANIES, EntityKind.COUNTRY, EntityKind.AUTHORITY, EntityKind.ORGANIZATION, EntityKind.SECTOR),
    ),
    RelationshipKind.COMPETES: ((*COMPANIES, EntityKind.PRODUCT), (*COMPANIES, EntityKind.PRODUCT)),
    RelationshipKind.REGULATES: (
        (EntityKind.AUTHORITY, EntityKind.ORGANIZATION),
        (*COMPANIES, EntityKind.PRODUCT, EntityKind.SECTOR),
    ),
    RelationshipKind.DEPENDS_ON: ((*COMPANIES, EntityKind.COUNTRY, EntityKind.SECTOR), (EntityKind.COMMODITY,)),
}
# What the other entity is to the one whose page shows it: (kind, the page's entity is the subject).
ROLE_LABELS = {
    (RelationshipKind.SUPPLIES, True): "Kunde",
    (RelationshipKind.SUPPLIES, False): "Lieferant",
    (RelationshipKind.COMPETES, True): "Wettbewerber",
    (RelationshipKind.COMPETES, False): "Wettbewerber",
    (RelationshipKind.REGULATES, True): "Reguliert",
    (RelationshipKind.REGULATES, False): "Regulator",
    (RelationshipKind.DEPENDS_ON, True): "Abhängig von",
    (RelationshipKind.DEPENDS_ON, False): "Abhängig davon",
}
# The roles the analyst chooses from: the other entity's role, the relationship's kind and whether this one is its
# subject.
ROLES = {
    "customer": ("Kunde", RelationshipKind.SUPPLIES, True),
    "supplier": ("Lieferant", RelationshipKind.SUPPLIES, False),
    "competitor": ("Wettbewerber", RelationshipKind.COMPETES, True),
    "regulated": ("Reguliert (von dieser Entität)", RelationshipKind.REGULATES, True),
    "regulator": ("Regulator", RelationshipKind.REGULATES, False),
    "commodity": ("Rohstoff, von dem sie abhängt", RelationshipKind.DEPENDS_ON, True),
    "dependent": ("Abhängig von diesem Rohstoff", RelationshipKind.DEPENDS_ON, False),
}
KIND_TITLES = {
    RelationshipKind.SUPPLIES: "liefert an",
    RelationshipKind.COMPETES: "konkurriert mit",
    RelationshipKind.REGULATES: "reguliert",
    RelationshipKind.DEPENDS_ON: "hängt ab von",
}
REVIEW_LABELS = {ReviewStatus.UNREVIEWED: "Ungeprüft", ReviewStatus.CONFIRMED: "Bestätigt", ReviewStatus.DISPUTED: "Bestritten"}


class InvalidRelationship(ValueError):
    """An Angabe of the analyst that cannot be: nothing is recorded."""


def allowed(kind: str, subject_kind: str, object_kind: str) -> bool:
    """Whether entities of these kinds can stand in the relationship."""
    subjects, objects = SIDES[kind]
    return subject_kind in subjects and object_kind in objects


def normalized(kind: str, subject_id: int, object_id: int) -> tuple[int, int, bool]:
    """Subject and object as stored, and whether they were swapped: a competition's subject is the lower id."""
    if kind == RelationshipKind.COMPETES and object_id < subject_id:
        return object_id, subject_id, True
    return subject_id, object_id, False


@dataclass(frozen=True)
class State:
    """A relationship's validity and review after its Angaben so far."""

    valid_from: date | None = None  # unknown
    valid_to: date | None = None  # open: it holds
    review: str = ReviewStatus.UNREVIEWED

    @property
    def in_effect(self) -> bool:
        """It holds, as far as known, and the analyst does not dispute it."""
        return self.valid_to is None and self.review != ReviewStatus.DISPUTED

    @property
    def validity_label(self) -> str:
        start = self.valid_from.strftime("%d.%m.%Y") if self.valid_from else "unbekannt"
        if self.valid_to is None:
            return f"seit {start}" if self.valid_from else "besteht, Beginn unbekannt"
        return f"{start} bis {self.valid_to.strftime('%d.%m.%Y')}"

    @property
    def review_label(self) -> str:
        return REVIEW_LABELS[self.review]


def apply(state: State | None, record) -> State:
    """The state after one more Angabe. The analyst's sets dates and review as given; a report's only dates it: that
    it holds opens it from its day - again after an end before that day - or moves its start earlier; that it ended
    closes it, while it is open and from its start on. A report's Angabe that changes nothing confirms."""
    if record.origin == RecordOrigin.ANALYST:
        return State(record.valid_from, record.valid_to, record.review or (state.review if state else ReviewStatus.UNREVIEWED))
    day = record.reported_on
    if state is None:
        return State(valid_to=day) if record.ended else State(valid_from=day)
    if day is None:
        return state
    if record.ended:
        if state.valid_to is None and (state.valid_from is None or day >= state.valid_from):
            return replace(state, valid_to=day)
        return state
    if state.valid_to is not None and day > state.valid_to:
        return replace(state, valid_from=day, valid_to=None)
    if (state.valid_from is None or day < state.valid_from) and (state.valid_to is None or day <= state.valid_to):
        return replace(state, valid_from=day)
    return state


def replay(records: Iterable) -> list[tuple[object, State, bool]]:
    """Each Angabe, in the order given, with the state after it and whether it changed the state."""
    steps, state = [], None
    for record in records:
        after = apply(state, record)
        steps.append((record, after, after != state))
        state = after
    return steps


def record_reported(
    connection: Connection, kind: str, subject: tuple[int, int], object_: tuple[int, int], candidate_id: int,
    statement_id: int, reported_on: date, ended: bool,
) -> None:
    """A report's Angabe, in the transaction of its recognition and under its lock. `subject` and `object_`: the entity
    and the name form the report names it by."""
    subject_id, object_id, swapped = normalized(kind, subject[0], object_[0])
    subject_alias, object_alias = (object_[1], subject[1]) if swapped else (subject[1], object_[1])
    relationship_id = relationship_repository.get_or_create(connection, kind, subject_id, object_id)
    relationship_repository.insert_record(connection, {
        "relationship_id": relationship_id, "origin": RecordOrigin.REPORT, "candidate_id": candidate_id,
        "statement_id": statement_id, "subject_alias_id": subject_alias, "object_alias_id": object_alias,
        "reported_on": reported_on, "ended": ended,
    })


def add(engine: Engine, entity_id: int, role: str, other: str, valid_from: str = "", valid_to: str = "", note: str = "") -> int:
    """The analyst's relationship of the entity with another, the other's role chosen, confirmed, with the dates given
    and a source - its id. InvalidRelationship when it cannot be."""
    if role not in ROLES:
        raise InvalidRelationship("Bitte eine Rolle wählen.")
    _, kind, is_subject = ROLES[role]
    start, end = _dates(valid_from, valid_to)
    note = _note(note, required=True)
    with engine.begin() as connection:
        entity_repository.lock_entities(connection)
        entity = _live(connection, entity_id)
        partner = _live(connection, _other_id(connection, other))
        if partner.id == entity.id:
            raise InvalidRelationship("Eine Beziehung braucht zwei verschiedene Entitäten.")
        subject, object_ = (entity, partner) if is_subject else (partner, entity)
        if not allowed(kind, subject.kind, object_.kind):
            raise InvalidRelationship(
                f"{entity_service.KIND_LABELS[subject.kind]} {KIND_TITLES[kind]} {entity_service.KIND_LABELS[object_.kind]}: "
                "Diese Beziehung ist so nicht vorgesehen."
            )
        subject_id, object_id, _ = normalized(kind, subject.id, object_.id)
        relationship_id = relationship_repository.get_or_create(connection, kind, subject_id, object_id)
        relationship_repository.insert_record(connection, {
            "relationship_id": relationship_id, "origin": RecordOrigin.ANALYST, "valid_from": start, "valid_to": end,
            "review": ReviewStatus.CONFIRMED, "note": note,
        })
    # IDs only, never a name or the note (datamodel.md, rule 13).
    logger.info("Relationship recorded by the analyst", extra={"relationship_id": relationship_id, "entity_id": entity_id})
    return relationship_id


def set_state(engine: Engine, relationship_id: int, valid_from: str, valid_to: str, review: str, note: str = "") -> None:
    """The analyst's Angabe of the relationship's validity and review. InvalidRelationship when it cannot be."""
    if review not in REVIEW_LABELS:
        raise InvalidRelationship("Bitte eine Prüfung wählen.")
    start, end = _dates(valid_from, valid_to)
    note = _note(note)
    with engine.begin() as connection:
        entity_repository.lock_entities(connection)
        if relationship_repository.get(connection, relationship_id) is None:
            raise InvalidRelationship("Beziehung nicht gefunden.")
        relationship_repository.insert_record(connection, {
            "relationship_id": relationship_id, "origin": RecordOrigin.ANALYST, "valid_from": start, "valid_to": end,
            "review": review, "note": note,
        })
    logger.info("Relationship state set by the analyst", extra={"relationship_id": relationship_id, "review": review})


def review(engine: Engine, entity_id: int, relationship_ids: Iterable[int], status: str, note: str = "") -> BulkResult:
    """Bestätigen or Bestreiten the selected relationships of the entity, their dates as they are - all or none."""
    note = _note(note)
    relationship_ids = sorted(set(relationship_ids))
    if not relationship_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        entity_repository.lock_entities(connection)
        own = {row.id for row in relationship_repository.of_entity(connection, entity_id)}
        blocked = [f"Beziehung {relationship_id} (nicht bei dieser Entität)" for relationship_id in relationship_ids if relationship_id not in own]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        states = _states(connection, relationship_ids)
        for relationship_id in relationship_ids:
            state = states[relationship_id]
            relationship_repository.insert_record(connection, {
                "relationship_id": relationship_id, "origin": RecordOrigin.ANALYST, "valid_from": state.valid_from,
                "valid_to": state.valid_to, "review": status, "note": note,
            })
    logger.info("Relationships reviewed", extra={"entity_id": entity_id, "relationships": relationship_ids, "review": status})
    return BulkResult(processed=len(relationship_ids), blocked=[])


def follow_merge(connection: Connection, merged_ids: list[int], into_id: int) -> None:
    """The Angaben of the merged entities' relationships go to the same relationships of the entity they went into.
    One between a merged entity and that entity stays where it is: no entity relates to itself."""
    merged = set(merged_ids)
    for relationship in relationship_repository.involving(connection, merged_ids):
        subject_id = into_id if relationship.subject_id in merged else relationship.subject_id
        object_id = into_id if relationship.object_id in merged else relationship.object_id
        _move(connection, relationship_repository.record_ids(connection, relationship.id), relationship.kind, subject_id, object_id)


def follow_split(connection: Connection, alias_ids: list[int], new_entity_id: int) -> None:
    """The reports' Angaben that name an entity by one of the name forms split off go to the same relationship of the
    entity they now make; the analyst's stay with the entity they were made for."""
    moved = set(alias_ids)
    for record in relationship_repository.reports_naming(connection, alias_ids):
        subject_id = new_entity_id if record.subject_alias_id in moved else record.subject_id
        object_id = new_entity_id if record.object_alias_id in moved else record.object_id
        _move(connection, [record.id], record.kind, subject_id, object_id)


def _move(connection: Connection, record_ids: list[int], kind: str, subject_id: int, object_id: int) -> None:
    if subject_id == object_id or not record_ids:
        return
    subject_id, object_id, swapped = normalized(kind, subject_id, object_id)
    target = relationship_repository.get_or_create(connection, kind, subject_id, object_id)
    relationship_repository.move_records(connection, record_ids, target, swap_sides=swapped)


def _states(connection: Connection, relationship_ids: list[int]) -> dict[int, State]:
    by_relationship: dict[int, list] = {relationship_id: [] for relationship_id in relationship_ids}
    for record in relationship_repository.records(connection, relationship_ids):
        by_relationship[record.relationship_id].append(record)
    return {relationship_id: (replay(rows)[-1][1] if rows else State()) for relationship_id, rows in by_relationship.items()}


def _live(connection: Connection, entity_id: int | None):
    entity = entity_repository.get(connection, entity_id) if entity_id is not None else None
    if entity is None:
        raise InvalidRelationship("Entität nicht gefunden.")
    if entity.merged_into_id is not None:
        raise InvalidRelationship(f"{entity.name} ist zusammengeführt in Entität {entity.merged_into_id}: bitte jene wählen.")
    return entity


def _other_id(connection: Connection, other: str) -> int | None:
    """The other entity as the form names it: "Name (Nr. 12)" as the list offers it, a number, or a name that only one
    entity has."""
    text = (other or "").strip()
    if not text:
        raise InvalidRelationship("Bitte die andere Entität angeben.")
    if text.endswith(")") and "(Nr. " in text:
        text = text.rsplit("(Nr. ", 1)[1][:-1]
    if text.isdigit():
        return int(text)
    found = entity_repository.live_by_name(connection, text)
    if len(found) > 1:
        raise InvalidRelationship(f"„{text}“ ist nicht eindeutig: Nr. {', '.join(str(each) for each in found)}.")
    return found[0] if found else None


def _dates(valid_from: str, valid_to: str) -> tuple[date | None, date | None]:
    try:
        start = date.fromisoformat(valid_from) if valid_from else None
        end = date.fromisoformat(valid_to) if valid_to else None
    except ValueError:
        raise InvalidRelationship("Ein Datum ist ungültig.") from None
    if start and end and end < start:
        raise InvalidRelationship("Das Ende liegt vor dem Beginn.")
    return start, end


def _note(text: str | None, required: bool = False) -> str | None:
    note = (text or "").strip()
    if required and not note:
        raise InvalidRelationship("Bitte eine Quelle angeben: einen Link oder eine Notiz.")
    if len(note) > MAX_NOTE_CHARS:
        raise InvalidRelationship(f"Die Quelle hat {len(note)} Zeichen, höchstens {MAX_NOTE_CHARS} sind möglich.")
    return note or None


@dataclass(frozen=True)
class RelationshipView:
    """A relationship as the page of one of its entities shows it."""

    id: int
    kind: str
    is_subject: bool  # the page's entity is its subject
    other: "entity_service.EntityRef"
    state: State
    report_count: int  # reports' Angaben
    uncertain: bool  # a report names one side by an uncertain name form

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[(self.kind, self.is_subject)]


@dataclass(frozen=True)
class RecordView:
    """An Angabe with the state after it."""

    record: object
    state: State
    changed: bool

    @property
    def origin_label(self) -> str:
        return "Bericht" if self.record.origin == RecordOrigin.REPORT else "Analyst"

    @property
    def says_label(self) -> str:
        """What the Angabe says."""
        record = self.record
        if record.origin == RecordOrigin.REPORT:
            return "beendet" if record.ended else "besteht"
        return f"{State(record.valid_from, record.valid_to).validity_label}, {REVIEW_LABELS[record.review]}"

    @property
    def source_quality_label(self) -> str:
        return f"Quelle {level_label(self.record.source_quality)}"


@dataclass(frozen=True)
class RelationshipDetail:
    id: int
    kind: str
    subject: "entity_service.EntityRef"
    object: "entity_service.EntityRef"
    state: State
    records: list[RecordView] = field(default_factory=list)  # the order Seismo learned them

    @property
    def kind_title(self) -> str:
        return KIND_TITLES[self.kind]


def of_entity(engine: Engine, entity_id: int) -> list[RelationshipView]:
    """The entity's relationships with Angaben, the ones in effect first, then by kind and the other's name."""
    with engine.connect() as connection:
        rows = relationship_repository.of_entity(connection, entity_id)
        records = relationship_repository.records(connection, [row.id for row in rows])
    by_relationship: dict[int, list] = {}
    for record in records:
        by_relationship.setdefault(record.relationship_id, []).append(record)
    views = []
    for row in rows:
        own = by_relationship.get(row.id, [])
        reports = [record for record in own if record.origin == RecordOrigin.REPORT]
        views.append(RelationshipView(
            id=row.id, kind=row.kind, is_subject=row.subject_id == entity_id,
            other=entity_service.EntityRef(row.other_id, row.other_name, row.other_kind), state=replay(own)[-1][1],
            report_count=len(reports), uncertain=any(record.subject_uncertain or record.object_uncertain for record in reports),
        ))
    return sorted(views, key=lambda view: not view.state.in_effect)


def detail(engine: Engine, relationship_id: int) -> RelationshipDetail | None:
    with engine.connect() as connection:
        row = relationship_repository.get(connection, relationship_id)
        if row is None:
            return None
        records = relationship_repository.records(connection, [relationship_id])
    steps = [RecordView(record, state, changed) for record, state, changed in replay(records)]
    return RelationshipDetail(
        row.id, row.kind, entity_service.EntityRef(row.subject_id, row.subject_name, row.subject_kind),
        entity_service.EntityRef(row.object_id, row.object_name, row.object_kind), steps[-1].state if steps else State(), steps,
    )


def role_choices() -> list[tuple[str, str]]:
    return [(role, label) for role, (label, _, _) in ROLES.items()]
