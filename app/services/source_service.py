"""Quellen: the news sources and the rules they are fetched by (userstories.md, Quellenadapter und Ingestion)."""
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine, Row

from app.repositories import source_repository
from app.repositories.source_repository import NameTaken
from app.services.bulk import BulkResult
from app.sources import ADAPTER_LABELS, ADAPTERS
from app.sources.base import SourceAdapter

# The source types the form offers: one per adapter in app/sources/.
ADAPTER_TYPES = ADAPTER_LABELS
# ISO 639-1 codes. No language means several or unknown ones, never a guess (datamodel.md, rule 5).
LANGUAGES = {
    "de": "Deutsch",
    "en": "Englisch",
    "fr": "Französisch",
    "it": "Italienisch",
    "es": "Spanisch",
    "pt": "Portugiesisch",
    "nl": "Niederländisch",
    "ru": "Russisch",
    "zh": "Chinesisch",
    "ja": "Japanisch",
    "ar": "Arabisch",
}
NO_LANGUAGE_LABEL = "Mehrsprachig oder unbekannt"
# What a source reports on, in the order the list sorts them (userstories.md, Quellenadapter und Ingestion, story 8).
REGIONS = {
    "africa": "Afrika",
    "asia": "Asien",
    "europe": "Europa",
    "middle_east": "Naher Osten",
    "north_america": "Nordamerika",
    "south_america": "Südamerika",
    "oceania": "Ozeanien",
    "worldwide": "Weltweit",
}
# The quality of a source's articles where no domain of the list in the Plattform-Einstellungen matches; empty: unknown.
QUALITIES = {"high": "hoch", "medium": "mittel", "low": "niedrig"}
NO_REGION = "none"  # the list's filter value for sources from before regions existed
NO_REGION_LABEL = "Ohne Region"
SORT_ORDERS = {"name": "Name", "region": "Region"}

MAX_NAME_LENGTH = 200
MAX_URL_LENGTH = 2000
MAX_TERMS_NOTE_LENGTH = 2000
MAX_QUERY_LENGTH = 1000
FETCH_INTERVAL_MINUTES = (5, 1440)
# Full texts are kept only temporarily (requirements.md, "Data Sources and Data Rights"); 0 deletes
# a raw text as soon as its processing has succeeded.
RAW_TEXT_RETENTION_DAYS = (0, 365)
# The range of the platform's "Mindestabstand pro Domain"; a source's own value only ever raises it.
MIN_REQUEST_INTERVAL_SECONDS = (1, 600)
DEFAULT_FETCH_INTERVAL_MINUTES = 60
DEFAULT_RAW_TEXT_RETENTION_DAYS = 30

# What the form for a new source starts with.
NEW_SOURCE_FORM = {
    "name": "",
    "adapter_type": "rss",
    "url": "",
    "query": "",
    "region": "",
    "is_active": True,
    "extract_events": True,
    "quality": "",
    "fetch_interval_minutes": DEFAULT_FETCH_INTERVAL_MINUTES,
    "language": "",
    "terms_note": "",
    "raw_text_retention_days": DEFAULT_RAW_TEXT_RETENTION_DAYS,
    "min_request_interval_seconds": "",
}
# Abrufstatus of a source: its CSS class (status-*) and label.
FETCH_STATUS_NEW = "new"
FETCH_STATUS_OK = "ok"
FETCH_STATUS_FAILING = "failing"


class InvalidSource(ValueError):
    """Invalid entries from the source form. The message is shown to the admin as is."""


@dataclass(frozen=True)
class SourceValues:
    """A source as the admin entered it, checked."""

    name: str
    adapter_type: str
    url: str
    query: str | None
    is_active: bool
    extract_events: bool  # its articles pass the event extraction's pre-filter (extraction_service)
    fetch_interval_minutes: int
    language: str | None
    terms_note: str | None
    raw_text_retention_days: int
    min_request_interval_seconds: int | None
    region: str | None  # always set by the form; None only for sources from before regions existed
    quality: str | None  # QUALITIES; None: unknown


@dataclass(frozen=True)
class Source(SourceValues):
    id: int
    created_at: datetime
    updated_at: datetime
    last_fetched_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    consecutive_failures: int

    @property
    def fetch_status(self) -> str:
        if self.consecutive_failures:
            return FETCH_STATUS_FAILING
        return FETCH_STATUS_OK if self.last_fetched_at else FETCH_STATUS_NEW

    @property
    def fetch_status_label(self) -> str:
        if self.consecutive_failures:
            return f"{self.consecutive_failures} Fehler in Folge"
        return "OK" if self.last_fetched_at else "Noch nicht abgerufen"

    @property
    def adapter_label(self) -> str:
        return ADAPTER_TYPES.get(self.adapter_type, self.adapter_type)

    @property
    def language_label(self) -> str:
        return LANGUAGES.get(self.language, self.language) if self.language else NO_LANGUAGE_LABEL

    @property
    def region_label(self) -> str:
        return REGIONS.get(self.region, self.region) if self.region else NO_REGION_LABEL


def parse(form: Mapping[str, str]) -> SourceValues:
    """Check every field of the source form; all errors together in one InvalidSource."""
    errors = []

    def checked(parse_field: Callable[..., object], *args: object) -> object:
        try:
            return parse_field(*args)
        except InvalidSource as error:
            errors.append(str(error))
            return None

    adapter = ADAPTERS.get(form.get("adapter_type", "").strip())
    values = SourceValues(
        name=checked(_name, form.get("name", "")),
        adapter_type=checked(_choice, form.get("adapter_type", ""), "Typ", ADAPTER_TYPES),
        # An adapter with a fixed address (GDELT's API) fills an empty URL.
        url=checked(_url, form.get("url", "").strip() or (adapter and adapter.default_url) or ""),
        query=checked(_query, form.get("query", ""), adapter),
        # A checkbox: the browser sends it only when it is ticked.
        is_active=bool(form.get("is_active")),
        extract_events=bool(form.get("extract_events")),
        fetch_interval_minutes=checked(
            _whole_number, form.get("fetch_interval_minutes", ""), "Abrufintervall (Minuten)", FETCH_INTERVAL_MINUTES
        ),
        language=checked(_choice, form.get("language", ""), "Sprache", LANGUAGES, True),
        region=checked(_choice, form.get("region", ""), "Region", REGIONS),
        quality=checked(_choice, form.get("quality", ""), "Quellenqualität", QUALITIES, True),
        terms_note=checked(_terms_note, form.get("terms_note", "")),
        raw_text_retention_days=checked(
            _whole_number, form.get("raw_text_retention_days", ""), "Rohtext aufbewahren (Tage)", RAW_TEXT_RETENTION_DAYS
        ),
        min_request_interval_seconds=checked(
            _whole_number,
            form.get("min_request_interval_seconds", ""),
            "Mindestabstand (Sekunden)",
            MIN_REQUEST_INTERVAL_SECONDS,
            True,
        ),
    )
    if errors:
        raise InvalidSource(" ".join(errors))
    return values


def list_sources(engine: Engine, region: str | None = None, sort: str | None = None) -> list[Source]:
    """The sources sorted by name as a German reader expects (requirements.md, "Lists and Ordering"), or by region
    first (in the order of REGIONS, those without one last); all of them, those of one region, or those without
    one (NO_REGION). An unknown filter or sort means none."""
    region_order = list(REGIONS) if sort == "region" else None
    with engine.connect() as connection:
        rows = source_repository.all_by_name(
            connection,
            region=region if region in REGIONS else None,
            without_region=region == NO_REGION,
            region_order=region_order,
        )
    return [_source(row) for row in rows]


def set_region(engine: Engine, source_ids: Iterable[int], region: str) -> BulkResult:
    """Give the selected sources one region, all or nothing; InvalidSource for a region that is none of REGIONS."""
    region = _choice(region, "Region", REGIONS)
    return _bulk(engine, source_ids, lambda connection, ids: source_repository.set_region(connection, ids, region))


def get(engine: Engine, source_id: int) -> Source | None:
    with engine.connect() as connection:
        row = source_repository.get(connection, source_id)
    return _source(row) if row else None


def insert(connection: Connection, values: SourceValues) -> int:
    """Add a checked source inside the caller's transaction; NameTaken when the name is in use."""
    return source_repository.insert_source(connection, asdict(values))


def create(engine: Engine, form: Mapping[str, str]) -> Source:
    values = parse(form)
    try:
        with engine.begin() as connection:
            return _source(source_repository.get(connection, insert(connection, values)))
    except NameTaken:
        raise InvalidSource(_name_taken(values.name)) from None


def update(engine: Engine, source_id: int, form: Mapping[str, str]) -> Source | None:
    """Save every field of the form; None when the source no longer exists."""
    values = parse(form)
    try:
        with engine.begin() as connection:
            if not source_repository.update_source(connection, source_id, asdict(values)):
                return None
            return _source(source_repository.get(connection, source_id))
    except NameTaken:
        raise InvalidSource(_name_taken(values.name)) from None


def delete(engine: Engine, source_ids: Iterable[int]) -> BulkResult:
    return _bulk(engine, source_ids, source_repository.delete_sources)


def set_active(engine: Engine, source_ids: Iterable[int], active: bool) -> BulkResult:
    return _bulk(engine, source_ids, lambda connection, ids: source_repository.set_active(connection, ids, active))


def _bulk(engine: Engine, source_ids: Iterable[int], action: Callable[[Connection, list[int]], object]) -> BulkResult:
    """Run an action on the selected sources, all or nothing.

    Atomic (requirements.md, "Destructive and Bulk Actions"): if any selected source is blocked, none is
    processed, and the result names the blockers.
    """
    source_ids = sorted(set(source_ids))
    if not source_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id for row in source_repository.lock_sources(connection, source_ids)}
        blocked = [f"Quelle #{source_id} (nicht gefunden)" for source_id in source_ids if source_id not in found]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        action(connection, source_ids)
    return BulkResult(processed=len(source_ids), blocked=[])


def _name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise InvalidSource("Name: bitte angeben.")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidSource(f"Name: höchstens {MAX_NAME_LENGTH} Zeichen.")
    return name


def _name_taken(name: str) -> str:
    return f"Name: „{name}“ ist bereits vergeben."


def _url(raw: str) -> str:
    url = raw.strip()
    error = InvalidSource("URL: bitte eine vollständige Adresse mit http:// oder https:// angeben.")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise error from None
    if parts.scheme not in ("http", "https") or not parts.hostname or any(char.isspace() for char in url):
        raise error
    if len(url) > MAX_URL_LENGTH:
        raise InvalidSource(f"URL: höchstens {MAX_URL_LENGTH} Zeichen.")
    return url


def _query(raw: str, adapter: SourceAdapter | None) -> str | None:
    """Required for an adapter that needs one, optional for one that accepts one, not allowed for the others;
    unchecked while the type is invalid."""
    query = raw.strip()
    if adapter is None:
        return query or None
    if not adapter.accepts_query:
        if query:
            types = ", ".join(each.label for each in ADAPTERS.values() if each.accepts_query)
            raise InvalidSource(f"Suchanfrage: nur für die Typen {types}.")
        return None
    if not query:
        if adapter.needs_query:
            raise InvalidSource("Suchanfrage: bitte angeben.")
        return None
    if len(query) > MAX_QUERY_LENGTH:
        raise InvalidSource(f"Suchanfrage: höchstens {MAX_QUERY_LENGTH} Zeichen.")
    return query


def _choice(raw: str, label: str, choices: Mapping[str, str], optional: bool = False) -> str | None:
    value = raw.strip()
    if optional and not value:
        return None
    if value not in choices:
        raise InvalidSource(f"{label}: bitte einen der angebotenen Werte wählen.")
    return value


def _whole_number(raw: str, label: str, limits: tuple[int, int], optional: bool = False) -> int | None:
    minimum, maximum = limits
    if optional and not raw.strip():
        return None
    empty = " oder leer lassen" if optional else ""
    error = InvalidSource(f"{label}: bitte eine ganze Zahl von {minimum} bis {maximum} eingeben{empty}.")
    try:
        value = int(raw.strip())
    except ValueError:
        raise error from None
    if not minimum <= value <= maximum:
        raise error
    return value


def _terms_note(raw: str) -> str | None:
    note = raw.strip()
    if len(note) > MAX_TERMS_NOTE_LENGTH:
        raise InvalidSource(f"Nutzungsbedingungen: höchstens {MAX_TERMS_NOTE_LENGTH} Zeichen.")
    return note or None


def _source(row: Row) -> Source:
    return Source(**{field.name: row._mapping[field.name] for field in fields(Source)})
