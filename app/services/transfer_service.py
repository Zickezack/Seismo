"""Export and import of the configuration: the Plattform-Einstellungen and the Quellen as one JSON file, to set up
a new instance or keep a copy of the settings (userstories.md, Admin / Settings UX story 3).

The file names its scope (requirements.md, "Settings", item 4) and holds no secrets: a Quelle carries the fields of
its form only, never an API key (source_service refuses one in a URL; keys stay in the environment), and no fetch
status. An import checks the whole file first; an invalid file changes nothing. Collected data - articles, events,
jobs - goes with the database backups (backup_service), not here.
"""
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime

from sqlalchemy import Engine

from app.repositories.source_repository import NameTaken
from app.services import platform_settings_service, source_service
from app.services.platform_settings_service import InvalidSetting
from app.services.source_service import InvalidSource, SourceValues

FORMAT = "seismo-export"
VERSION = 1
SETTINGS = "platform_settings"
SOURCES = "sources"
SCOPE_LABELS = {SETTINGS: "Plattform-Einstellungen", SOURCES: "Quellen"}
# A configuration is small: the starter catalogue's sources take some 30 KB. A larger file is no export of ours.
MAX_FILE_BYTES = 5 * 1024 * 1024
SOURCE_FIELDS = tuple(field.name for field in fields(SourceValues))
# What a Quelle without a field gets: the new source form's defaults, so a hand-written file needs only the
# required ones.
SOURCE_DEFAULTS = {name: source_service.NEW_SOURCE_FORM.get(name, "") for name in SOURCE_FIELDS}


class InvalidImport(ValueError):
    """The file cannot be imported; nothing was changed. The message is shown to the admin as is."""


@dataclass(frozen=True)
class ExportFile:
    filename: str
    content: bytes


@dataclass(frozen=True)
class ExportResult:
    file: ExportFile | None  # None when an item blocked the export
    blocked: list[str]


@dataclass(frozen=True)
class ImportResult:
    settings_taken: bool
    sources_imported: int
    name_taken: list[str]  # Quellen skipped because the name is in use here


def export(engine: Engine, include_settings: bool, include_sources: bool, now: datetime | None = None) -> ExportFile:
    """The Plattform-Einstellungen, every Quelle, or both."""
    if not (include_settings or include_sources):
        raise ValueError("nothing to export")
    settings = platform_settings_service.current(engine) if include_settings else None
    sources = source_service.list_sources(engine) if include_sources else None
    return _file(settings, sources, now)


def export_sources(engine: Engine, source_ids: Iterable[int], now: datetime | None = None) -> ExportResult:
    """The selected Quellen, all or nothing (requirements.md, "Destructive and Bulk Actions", item 2)."""
    source_ids = sorted(set(source_ids))
    if not source_ids:
        return ExportResult(None, [])
    sources = [source for source in source_service.list_sources(engine) if source.id in source_ids]
    found = {source.id for source in sources}
    blocked = [f"Quelle #{source_id} (nicht gefunden)" for source_id in source_ids if source_id not in found]
    if blocked:
        return ExportResult(None, blocked)
    return ExportResult(_file(None, sources, now), [])


def import_file(engine: Engine, content: bytes, take_settings: bool, take_sources: bool) -> ImportResult:
    """Take the chosen parts of an export file. A Quelle whose name is in use here is skipped, never overwritten,
    so importing a file twice changes nothing the second time."""
    if not (take_settings or take_sources):
        raise InvalidImport("Bitte Einstellungen, Quellen oder beides wählen.")
    document = _document(content)
    settings = _settings(document) if take_settings else None
    sources = _sources(document) if take_sources else []
    imported, name_taken = 0, []
    with engine.begin() as connection:
        if settings is not None:
            platform_settings_service.store(connection, settings)
        for values in sources:
            try:
                # A savepoint: a name in use must not end the transaction of the other sources.
                with connection.begin_nested():
                    source_service.insert(connection, values)
            except NameTaken:
                name_taken.append(values.name)
                continue
            imported += 1
    return ImportResult(settings is not None, imported, name_taken)


def _file(settings, sources, now: datetime | None) -> ExportFile:
    now = now or datetime.now(UTC)
    document: dict[str, object] = {"format": FORMAT, "version": VERSION, "exported_at": now.isoformat()}
    scope = [name for name, part in ((SETTINGS, settings), (SOURCES, sources)) if part is not None]
    document["scope"] = scope
    if settings is not None:
        document[SETTINGS] = asdict(settings)
    if sources is not None:
        document[SOURCES] = [{name: getattr(source, name) for name in SOURCE_FIELDS} for source in sources]
    parts = "-".join({SETTINGS: "einstellungen", SOURCES: "quellen"}[name] for name in scope)
    filename = f"seismo-{parts}-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    return ExportFile(filename, json.dumps(document, ensure_ascii=False, indent=2).encode())


def _document(content: bytes) -> dict:
    if len(content) > MAX_FILE_BYTES:
        raise InvalidImport(f"Die Datei ist grösser als {MAX_FILE_BYTES // (1024 * 1024)} MB, kein Export von Seismo.")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidImport("Die Datei ist kein JSON, kein Export von Seismo.") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise InvalidImport("Die Datei ist kein Export von Seismo.")
    if document.get("version") != VERSION:
        raise InvalidImport(f"Exportformat Version {document.get('version')!r} wird nicht unterstützt, nur {VERSION}.")
    return document


def _settings(document: dict) -> dict[str, object]:
    settings = document.get(SETTINGS)
    if settings is None:
        raise InvalidImport("Die Datei enthält keine Plattform-Einstellungen.")
    if not isinstance(settings, dict):
        raise InvalidImport("Plattform-Einstellungen: kein Objekt.")
    unknown = sorted(set(settings) - set(platform_settings_service.FORM_FIELDS))
    if unknown:
        raise InvalidImport(f"Plattform-Einstellungen: unbekannt {', '.join(unknown)}.")
    try:
        return platform_settings_service.parse(_form(settings, "Plattform-Einstellungen"))
    except InvalidSetting as error:
        raise InvalidImport(f"Plattform-Einstellungen: {error}") from None


def _sources(document: dict) -> list[SourceValues]:
    items = document.get(SOURCES)
    if items is None:
        raise InvalidImport("Die Datei enthält keine Quellen.")
    if not isinstance(items, list):
        raise InvalidImport("Quellen: keine Liste.")
    values, errors, names = [], [], {}
    for number, item in enumerate(items, start=1):
        label = f"Quelle {number}"
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            label += f" („{item['name'].strip()}“)"
        try:
            source = _source(item, label)
        except InvalidImport as error:
            errors.append(str(error))
            continue
        # The same rule as the database's unique index: a name regardless of case.
        if (key := source.name.lower()) in names:
            errors.append(f"{label}: der Name kommt schon als Quelle {names[key]} vor.")
            continue
        names[key] = number
        values.append(source)
    if errors:
        raise InvalidImport(" ".join(errors))
    return values


def _source(item: object, label: str) -> SourceValues:
    if not isinstance(item, dict):
        raise InvalidImport(f"{label}: kein Objekt.")
    unknown = sorted(set(item) - set(SOURCE_FIELDS))
    if unknown:
        raise InvalidImport(f"{label}: unbekannt {', '.join(unknown)}.")
    try:
        return source_service.parse(_form(SOURCE_DEFAULTS | item, label))
    except InvalidSource as error:
        raise InvalidImport(f"{label}: {error}") from None


def _form(values: Mapping[str, object], label: str) -> dict[str, str]:
    """JSON values as the form sends them: a checkbox only when ticked, an empty field for null."""
    form, wrong = {}, []
    for name, value in values.items():
        if isinstance(value, bool):
            if value:
                form[name] = "on"
        elif value is None:
            form[name] = ""
        elif isinstance(value, (int, str)):
            form[name] = str(value)
        else:
            wrong.append(name)
    if wrong:
        raise InvalidImport(f"{label}: kein Text, keine Zahl und kein Wahrheitswert: {', '.join(wrong)}.")
    return form
