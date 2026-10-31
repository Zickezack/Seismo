"""Seed data: imported only by an explicit admin action, each item once (architecture.md, "Seed Data")."""
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from app.db.models import AliasMatch, AliasScheme
from app.repositories import entity_repository, seed_log_repository, source_repository
from app.repositories.source_repository import NameTaken
from app.services import source_service
from app.services.entity_names import name_key
from app.services.source_service import SourceValues

SEEDS_DIR = Path(__file__).resolve().parents[1] / "seeds"
# Key of the starter catalogue of Quellen in the seed log, and the name of its file in SEEDS_DIR.
SOURCES_SEED = "sources"
# The same of the starter list of sectors and commodities (Entitäten).
ENTITIES_SEED = "entities"


@dataclass(frozen=True)
class SeedItem:
    key: str  # stable: never changes, independent of the display name
    values: SourceValues


@dataclass(frozen=True)
class SeedResult:
    imported: int
    skipped: int  # imported by an earlier run; whether renamed or deleted since, they stay as they are
    name_taken: list[str]  # names another source already has; a later run imports them once the name is free


@dataclass(frozen=True)
class SeedStatus:
    total: int
    imported: int


def source_catalogue() -> list[SeedItem]:
    """The starter catalogue of Quellen, checked by the same rules as the source form."""
    items = json.loads((SEEDS_DIR / f"{SOURCES_SEED}.json").read_text(encoding="utf-8"))
    return [SeedItem(key=item["key"], values=source_service.parse(_form_of(item))) for item in items]


def sources_status(engine: Engine) -> SeedStatus:
    keys = {item.key for item in source_catalogue()}
    with engine.connect() as connection:
        imported = seed_log_repository.imported_keys(connection, SOURCES_SEED)
    return SeedStatus(total=len(keys), imported=len(keys & imported))


def import_sources(engine: Engine) -> SeedResult:
    """Import every catalogue item not imported before, each in one transaction with its log entry."""
    imported, skipped, name_taken = 0, 0, []
    for item in source_catalogue():
        with engine.begin() as connection:
            if not seed_log_repository.record(connection, SOURCES_SEED, item.key):
                skipped += 1
                continue
            try:
                source_service.insert(connection, item.values)
            except NameTaken:
                # Not imported, so not logged either.
                seed_log_repository.forget(connection, SOURCES_SEED, item.key)
                name_taken.append(item.values.name)
                continue
            imported += 1
    return SeedResult(imported=imported, skipped=skipped, name_taken=name_taken)


def entity_catalogue() -> list[dict]:
    """The starter list of sectors and commodities: key, kind, German name, and what it covers in English - what the
    entity recognition offers the LLM (entity_service)."""
    return json.loads((SEEDS_DIR / f"{ENTITIES_SEED}.json").read_text(encoding="utf-8"))


def entities_status(engine: Engine) -> SeedStatus:
    keys = {item["key"] for item in entity_catalogue()}
    with engine.connect() as connection:
        imported = seed_log_repository.imported_keys(connection, ENTITIES_SEED)
    return SeedStatus(total=len(keys), imported=len(keys & imported))


def import_entities(engine: Engine) -> SeedResult:
    """Import every sector and commodity of the starter list not imported before, each in one transaction with its log
    entry: an entity with its key and its German and English names as name forms. A key an entity has already - its
    log entry lost - is neither imported nor logged again."""
    imported, skipped, name_taken = 0, 0, []
    for item in entity_catalogue():
        with engine.begin() as connection:
            entity_repository.lock_entities(connection)
            if not seed_log_repository.record(connection, ENTITIES_SEED, item["key"]):
                skipped += 1
                continue
            if entity_repository.identifier_taken(connection, AliasScheme.CATALOGUE, item["key"]):
                seed_log_repository.forget(connection, ENTITIES_SEED, item["key"])
                name_taken.append(item["name"])
                continue
            entity_id = entity_repository.create_entity(connection, {"kind": item["kind"], "name": item["name"]})
            aliases = {(AliasScheme.CATALOGUE, item["key"]): item["key"]}
            for form in (item["name"], item["english"].split(" (")[0]):
                aliases.setdefault((AliasScheme.NAME, name_key(form)), form)
            for (scheme, key), value in aliases.items():
                values = {"entity_id": entity_id, "scheme": scheme, "value": value, "key": key, "match": AliasMatch.SEED}
                entity_repository.insert_alias(connection, values)
            imported += 1
    return SeedResult(imported=imported, skipped=skipped, name_taken=name_taken)


def take_catalogue_regions(engine: Engine) -> int:
    """Give every source the region of the catalogue item it matches - by URL and, for GDELT, its query - where it
    has none yet: sources from before regions existed (story 8). A region set by the admin stays. The number given one.

    An explicit admin action like the import (architecture.md, "Seed Data"); it imports nothing.
    """
    with engine.begin() as connection:
        return sum(
            source_repository.fill_region(connection, item.values.url, item.values.query, item.values.region)
            for item in source_catalogue()
        )


def _form_of(item: dict) -> dict[str, str]:
    """A catalogue item as the source form would send it. Imported sources are active (admin's decision, 2026-10-23)."""
    form = {
        "adapter_type": "rss",
        "fetch_interval_minutes": str(source_service.DEFAULT_FETCH_INTERVAL_MINUTES),
        "raw_text_retention_days": str(source_service.DEFAULT_RAW_TEXT_RETENTION_DAYS),
        "language": "",
        "terms_note": "",
    }
    form |= {name: str(value) for name, value in item.items() if name != "key"}
    return form | {"is_active": "on", "extract_events": "on"}
