"""Seed data: imported only by an explicit admin action, each item once (architecture.md, "Seed Data")."""
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from app.repositories import seed_log_repository
from app.repositories.source_repository import NameTaken
from app.services import source_service
from app.services.source_service import SourceValues

SEEDS_DIR = Path(__file__).resolve().parents[1] / "seeds"
# Key of the starter catalogue of Quellen in the seed log, and the name of its file in SEEDS_DIR.
SOURCES_SEED = "sources"


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
    return form | {"is_active": "on"}
