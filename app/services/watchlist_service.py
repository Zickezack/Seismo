"""Watchlisten (userstories.md, Dashboard, Recherche und Alerts, story 3): what an analyst follows - entities, event
types, countries, single events and Wertpapiere. An event matches a watchlist when it is one of its events, or it or
one of its reports has one of its types or countries, or a report names one of its entities or the one a Wertpapier of
it is linked to (event_repository._watched). The event list and the start page filter by a watchlist; an alert rule
covers the events of its watchlist (alert_service).

Entities, events and Wertpapiere are added from their own pages, types and countries on the watchlist's page.
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, Row

from app.db.models import WatchItemKind
from app.repositories import entity_repository, event_repository, instrument_repository, watchlist_repository
from app.repositories.watchlist_repository import NameTaken
from app.services.bulk import BulkResult
from app.services.entity_service import KIND_LABELS as ENTITY_KIND_LABELS
from app.services.extraction_service import EVENT_TYPE_LABELS

MAX_NAME_CHARS = 200
_COUNTRY = re.compile(r"[A-Z]{2}")
KIND_LABELS = {
    WatchItemKind.ENTITY: "Entität", WatchItemKind.EVENT_TYPE: "Ereignistyp", WatchItemKind.COUNTRY: "Land",
    WatchItemKind.EVENT: "Ereignis", WatchItemKind.INSTRUMENT: "Wertpapier",
}
# The order of the watchlist page's groups.
KIND_ORDER = (
    WatchItemKind.ENTITY, WatchItemKind.INSTRUMENT, WatchItemKind.EVENT, WatchItemKind.EVENT_TYPE, WatchItemKind.COUNTRY,
)
# Added from their own pages, by id.
_BY_ID = {
    WatchItemKind.ENTITY: (entity_repository.get, "Entität"),
    WatchItemKind.EVENT: (event_repository.get, "Ereignis"),
    WatchItemKind.INSTRUMENT: (instrument_repository.get, "Wertpapier"),
}


class InvalidWatchlist(ValueError):
    """An invalid entry for a watchlist. The message is shown to the analyst as is."""


@dataclass(frozen=True)
class WatchlistRef:
    id: int
    name: str


@dataclass(frozen=True)
class WatchlistListItem:
    id: int
    name: str
    item_count: int
    rule_count: int
    updated_at: datetime


@dataclass(frozen=True)
class WatchItemView:
    """One thing a watchlist follows, with what its page shows and links to."""

    id: int
    kind: str
    label: str
    href: str | None
    note: str = ""  # merged since, or how a Wertpapier counts

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]


@dataclass(frozen=True)
class WatchlistDetail:
    watchlist: WatchlistListItem
    items: list[WatchItemView]

    def of_kind(self, kind: str) -> list[WatchItemView]:
        return [item for item in self.items if item.kind == kind]


def parse_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise InvalidWatchlist("Name: bitte angeben.")
    if len(name) > MAX_NAME_CHARS:
        raise InvalidWatchlist(f"Name: höchstens {MAX_NAME_CHARS} Zeichen.")
    return name


def create(engine: Engine, raw_name: str | None) -> WatchlistRef:
    name = parse_name(raw_name)
    try:
        with engine.begin() as connection:
            return WatchlistRef(watchlist_repository.insert_watchlist(connection, name), name)
    except NameTaken:
        raise InvalidWatchlist(_name_taken(name)) from None


def rename(engine: Engine, watchlist_id: int, raw_name: str | None) -> WatchlistRef | None:
    """None when the watchlist no longer exists."""
    name = parse_name(raw_name)
    try:
        with engine.begin() as connection:
            if not watchlist_repository.rename(connection, watchlist_id, name):
                return None
    except NameTaken:
        raise InvalidWatchlist(_name_taken(name)) from None
    return WatchlistRef(watchlist_id, name)


def overview(engine: Engine) -> list[WatchlistListItem]:
    with engine.connect() as connection:
        return [_list_item(row) for row in watchlist_repository.listing(connection)]


def choices(engine: Engine) -> list[WatchlistRef]:
    """The watchlists a form offers to choose from, by name."""
    return [WatchlistRef(each.id, each.name) for each in overview(engine)]


def detail(engine: Engine, watchlist_id: int) -> WatchlistDetail | None:
    with engine.connect() as connection:
        row = next(iter(watchlist_repository.listing(connection, watchlist_id)), None)
        if row is None:
            return None
        rows = watchlist_repository.items(connection, watchlist_id)
    items = sorted((_item(each) for each in rows), key=lambda item: KIND_ORDER.index(item.kind))
    return WatchlistDetail(_list_item(row), items)


@dataclass(frozen=True)
class Watching:
    """What the page of an entity, an event or a Wertpapier offers: every watchlist, and those that follow it."""

    watchlists: list[WatchlistRef]
    following: list[int]


def watching(engine: Engine, kind: str, target_id: int) -> Watching:
    with engine.connect() as connection:
        following = watchlist_repository.containing(connection, kind, target_id)
        watchlists = [WatchlistRef(row.id, row.name) for row in watchlist_repository.listing(connection)]
    return Watching(watchlists, following)


def add(engine: Engine, watchlist_id: int, kind: str, raw: str | int | None) -> tuple[str, bool]:
    """Follow an entity, event or Wertpapier by its id, an event type by its key, a country by its ISO code. Returns
    the watchlist's name and whether it is new to it; InvalidWatchlist for what does not exist."""
    with engine.begin() as connection:
        watchlist = watchlist_repository.get(connection, watchlist_id)
        if watchlist is None:
            raise InvalidWatchlist("Watchlist nicht gefunden.")
        target = _target(connection, kind, raw)
        return watchlist.name, watchlist_repository.add_item(connection, watchlist_id, kind, target)


def _target(connection, kind: str, raw: str | int | None) -> int | str:
    if kind in _BY_ID:
        get, label = _BY_ID[kind]
        text = str(raw or "").strip()
        if not text.isdigit() or get(connection, int(text)) is None:
            raise InvalidWatchlist(f"{label} nicht gefunden.")
        return int(text)
    if kind == WatchItemKind.EVENT_TYPE:
        if raw not in EVENT_TYPE_LABELS:
            raise InvalidWatchlist("Ereignistyp: bitte einen aus der Liste wählen.")
        return str(raw)
    if kind == WatchItemKind.COUNTRY:
        code = str(raw or "").strip().upper()
        if not _COUNTRY.fullmatch(code):
            raise InvalidWatchlist("Land: bitte den zweistelligen ISO-Code angeben, z. B. CH.")
        return code
    raise InvalidWatchlist("Unbekannte Art.")


def remove_items(engine: Engine, watchlist_id: int, item_ids: Iterable[int]) -> BulkResult:
    """All or nothing: one that is not (or no longer) on the watchlist blocks all."""
    item_ids = sorted(set(item_ids))
    if not item_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = set(watchlist_repository.lock_items(connection, watchlist_id, item_ids))
        blocked = [f"Eintrag #{each} (nicht gefunden)" for each in item_ids if each not in found]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        watchlist_repository.remove_items(connection, watchlist_id, item_ids)
    return BulkResult(processed=len(item_ids), blocked=[])


def delete(engine: Engine, watchlist_ids: Iterable[int]) -> BulkResult:
    """All or nothing: one an alert rule uses blocks all - the rule would cover other events than it says."""
    watchlist_ids = sorted(set(watchlist_ids))
    if not watchlist_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id: row.name for row in watchlist_repository.lock_watchlists(connection, watchlist_ids)}
        blocked = [f"Watchlist #{each} (nicht gefunden)" for each in watchlist_ids if each not in found]
        users: dict[int, list[str]] = {}
        for rule in watchlist_repository.rules_using(connection, watchlist_ids):
            users.setdefault(rule.watchlist_id, []).append(rule.name)
        blocked += [f"{found[each]} (von der Alert-Regel {', '.join(names)} verwendet)" for each, names in users.items()]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        watchlist_repository.delete_watchlists(connection, watchlist_ids)
    return BulkResult(processed=len(watchlist_ids), blocked=[])


def _name_taken(name: str) -> str:
    return f"Name: „{name}“ ist bereits vergeben."


def _list_item(row: Row) -> WatchlistListItem:
    return WatchlistListItem(row.id, row.name, row.item_count, row.rule_count, row.updated_at)


def _item(row: Row) -> WatchItemView:
    if row.kind == WatchItemKind.ENTITY:
        note = f"zusammengeführt in {row.entity_merged_into_name}" if row.entity_merged_into_id else ENTITY_KIND_LABELS[row.entity_kind]
        return WatchItemView(row.id, row.kind, row.entity_name, f"/entities/{row.entity_id}", note)
    if row.kind == WatchItemKind.EVENT:
        note = f"zusammengeführt in Ereignis {row.event_merged_into_id}" if row.event_merged_into_id else f"Ereignis-ID {row.event_id}"
        return WatchItemView(row.id, row.kind, row.event_title, f"/events/{row.event_id}", note)
    if row.kind == WatchItemKind.INSTRUMENT:
        note = (
            f"zählt über die Entität {row.instrument_entity_name}" if row.instrument_entity_id
            else "ohne Entität: kein Ereignis passt dazu"
        )
        return WatchItemView(row.id, row.kind, f"{row.instrument_name} ({row.instrument_symbol})", f"/instruments/{row.instrument_id}", note)
    if row.kind == WatchItemKind.EVENT_TYPE:
        return WatchItemView(row.id, row.kind, EVENT_TYPE_LABELS.get(row.value, row.value), None)
    return WatchItemView(row.id, row.kind, row.value, None)
