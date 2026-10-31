"""The analyst's corrections of the entities (userstories.md, Entitäten und Wissensmodell, story 1): an uncertain name
form confirmed, name forms split off into an entity of their own, entities merged into one (datamodel.md, rule 9:
never merged silently - by the analyst only).

A name form carries its reports: moved, they name the entity it went to. A merged entity keeps its id and page, without
name forms, marked as merged into the other; its subsidiaries and products go with it. Every correction runs under the
lock of the recognition (entity_repository.lock_entities), so a name form is never moved while a recognition adds to
its entity. Sectors and commodities of the starter list keep their key: it is never split off.
"""
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Engine

from app.db.models import AliasMatch, AliasScheme
from app.repositories import entity_repository
from app.services import entity_service
from app.services.bulk import BulkResult
from app.services.entity_service import EntityListItem, matched_kinds

logger = logging.getLogger(__name__)

# Shown on the entity page: enough to find the entity a recognition missed, few enough to read.
SIMILAR_LIMIT = 10


@dataclass(frozen=True)
class SplitResult(BulkResult):
    entity_id: int | None = None  # the new entity


@dataclass(frozen=True)
class SimilarEntity:
    entity: EntityListItem
    score: float

    @property
    def score_label(self) -> str:
        return f"{self.score:.2f}".replace(".", ",")


def similar_entities(engine: Engine, entity_id: int) -> list[SimilarEntity]:
    """The other entities of its kind - a company's subsidiaries too - with a similar name form (pg_trgm), the most
    similar first: what the analyst merges from. None for a merged entity."""
    with engine.connect() as connection:
        entity = entity_repository.get(connection, entity_id)
        if entity is None or entity.merged_into_id is not None:
            return []
        found = entity_repository.similar_to_entity(connection, entity_id, matched_kinds(entity.kind), SIMILAR_LIMIT)
    items = {item.id: item for item in entity_service.listed(engine, [other for other, _ in found])}
    return [SimilarEntity(items[other], score) for other, score in found if other in items]


def confirm(engine: Engine, entity_id: int, alias_ids: Iterable[int]) -> BulkResult:
    """Bestätigen: the selected uncertain name forms are the entity's - all or none; a certain one selected stays as it
    is. The number confirmed."""
    alias_ids = sorted(set(alias_ids))
    if not alias_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        entity_repository.lock_entities(connection)
        aliases = entity_repository.aliases_by_id(connection, alias_ids)
        blocked = [blocker for alias_id in alias_ids if (blocker := _alias_blocker(aliases.get(alias_id), alias_id, entity_id))]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        uncertain = [alias_id for alias_id in alias_ids if aliases[alias_id].uncertain]
        entity_repository.confirm(connection, uncertain)
    logger.info("Name forms confirmed", extra={"entity_id": entity_id, "aliases": uncertain})
    return BulkResult(processed=len(uncertain), blocked=[])


def split(engine: Engine, entity_id: int, alias_ids: Iterable[int]) -> SplitResult:
    """Abtrennen: the selected name forms and identifiers become one new entity of the same kind, named after the
    earliest name form among them, certain - all or none. At least one name form must go and one stay; a key of the
    starter list never goes."""
    alias_ids = sorted(set(alias_ids))
    if not alias_ids:
        return SplitResult(processed=0, blocked=[])
    with engine.begin() as connection:
        entity_repository.lock_entities(connection)
        entity = entity_repository.get(connection, entity_id)
        if entity is None or entity.merged_into_id is not None:
            return SplitResult(processed=0, blocked=[_entity_blocker(entity, entity_id)])
        aliases = entity_repository.aliases_by_id(connection, alias_ids)
        blocked = [blocker for alias_id in alias_ids if (blocker := _alias_blocker(aliases.get(alias_id), alias_id, entity_id))]
        moving = [aliases[alias_id] for alias_id in alias_ids if alias_id in aliases]
        blocked += [f"{alias.value} (Starterliste)" for alias in moving if alias.scheme == AliasScheme.CATALOGUE]
        names = [alias for alias in moving if alias.scheme == AliasScheme.NAME]
        remaining = [key for (scheme, key), row in entity_repository.alias_keys(connection, entity_id).items()
                     if scheme == AliasScheme.NAME and row.id not in alias_ids]
        if not blocked and not names:
            blocked.append("keine Namensform ausgewählt: die neue Entität braucht einen Namen")
        if not blocked and not remaining:
            blocked.append(f"{entity.name} (behielte keine Namensform)")
        if blocked:
            return SplitResult(processed=0, blocked=blocked)
        name = min(names, key=lambda alias: alias.id).value
        new_id = entity_repository.create_entity(connection, {"kind": entity.kind, "name": name})
        values = {"match": AliasMatch.SPLIT, "uncertain": False, "similarity": None}
        entity_repository.move_aliases(connection, alias_ids, new_id, values)
    # IDs and counts only, never a name (datamodel.md, rule 13).
    logger.info("Name forms split off", extra={"entity_id": entity_id, "new_entity_id": new_id, "aliases": len(alias_ids)})
    return SplitResult(processed=len(alias_ids), blocked=[], entity_id=new_id)


def merge(engine: Engine, entity_id: int, other_ids: Iterable[int]) -> BulkResult:
    """Zusammenführen: the name forms, identifiers, reports, subsidiaries and products of the other entities go to
    this one - all or none, of its kind only (a company's subsidiaries too). Each stays as an entity merged into this
    one; this one takes the first parent among them should it have none."""
    other_ids = sorted(set(other_ids) - {entity_id})
    if not other_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        entity_repository.lock_entities(connection)
        rows = entity_repository.entities(connection, [entity_id, *other_ids])
        blocked = [blocker for each in [entity_id, *other_ids] if (blocker := _entity_blocker(rows.get(each), each))]
        if not blocked:
            kinds = matched_kinds(rows[entity_id].kind)
            blocked = [f"{rows[other].name} ({entity_service.KIND_LABELS[rows[other].kind]})" for other in other_ids
                       if rows[other].kind not in kinds]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        entity_repository.take_parent(connection, entity_id, other_ids)
        moved = entity_repository.merge_into(connection, other_ids, entity_id)
    logger.info("Entities merged", extra={"entity_id": entity_id, "merged": other_ids, "aliases": moved})
    return BulkResult(processed=len(other_ids), blocked=[])


def _alias_blocker(alias, alias_id: int, entity_id: int) -> str | None:
    if alias is None:
        return f"Namensform {alias_id} (nicht gefunden)"
    if alias.entity_id != entity_id:
        return f"{alias.value} (nicht bei dieser Entität)"
    return None


def _entity_blocker(entity, entity_id: int) -> str | None:
    """What keeps an entity from a correction: gone, or merged into another already."""
    if entity is None:
        return f"Entität {entity_id} (nicht gefunden)"
    if entity.merged_into_id is not None:
        return f"{entity.name} (bereits zusammengeführt)"
    return None
