"""Wertpapiere (userstories.md, Live-Kurse und Kursdatenspeicher, story 1): the stocks, ETFs and indices whose prices
the worker fetches (price_service), entered by the admin or imported with the starter list, each linked to the
Entität it belongs to - found by its ISIN among the name forms, or chosen. Every price is shown with its time, source,
exchange, currency and delay (requirements.md, Live-Kurse und Kursdatenspeicher, criterion 1).
"""
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, Engine, Row

from app.db.models import AliasScheme, InstrumentKind
from app.prices import eodhd
from app.repositories import entity_repository, instrument_repository, price_repository
from app.services.bulk import BulkResult
from app.services.entity_names import identifier_key

logger = logging.getLogger(__name__)

KIND_LABELS = {InstrumentKind.STOCK: "Aktie", InstrumentKind.ETF: "ETF", InstrumentKind.INDEX: "Index"}
# The exchange codes of the price source a page names in words; any other as it is.
EXCHANGE_LABELS = {
    "US": "US-Börsen", "SW": "SIX Swiss Exchange", "XETRA": "Xetra", "F": "Börse Frankfurt", "LSE": "London Stock Exchange",
    "PA": "Euronext Paris", "AS": "Euronext Amsterdam", "MI": "Borsa Italiana", "MC": "Bolsa de Madrid", "VI": "Wiener Börse",
    "TO": "Toronto Stock Exchange", "INDX": "Index",
}
SOURCE_LABELS = {eodhd.NAME: eodhd.LABEL}
DELAY_LABELS = {eodhd.NAME: eodhd.DELAY_LABEL}
RECENT_LIMIT = 100
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-_^]{0,29}")
_EXCHANGE = re.compile(r"[A-Z0-9]{1,20}")
_CURRENCY = re.compile(r"[A-Z]{3}")
MAX_NAME_CHARS = 200


class InvalidInstrument(ValueError):
    """Invalid entries from the Wertpapier form. The message is shown to the admin as is."""


def number_label(value: Decimal | None) -> str:
    """A price as the pages show it: a thin space between thousands, a decimal comma, two places - four below one."""
    if value is None:
        return "–"
    places = 4 if abs(value) < 1 else 2
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


@dataclass(frozen=True)
class PriceView:
    """A price with everything a page shows of it."""

    close: Decimal
    previous_close: Decimal | None
    at: datetime
    received_at: datetime
    source: str
    delayed: bool
    currency: str | None  # None: an index, in points
    exchange: str

    @property
    def price_label(self) -> str:
        return f"{number_label(self.close)} {self.currency or 'Punkte'}"

    @property
    def change_label(self) -> str:
        """Against the last trading day's close."""
        if not self.previous_close:
            return ""
        change = (self.close - self.previous_close) / self.previous_close * 100
        return f"{'+' if change > 0 else ''}{change:.2f} %".replace(".", ",")

    @property
    def exchange_label(self) -> str:
        return EXCHANGE_LABELS.get(self.exchange, self.exchange)

    @property
    def source_label(self) -> str:
        """The source and whether it is real time - every price says so."""
        delay = DELAY_LABELS.get(self.source, "verzögert") if self.delayed else "Echtzeit"
        return f"{SOURCE_LABELS.get(self.source, self.source)}, {delay}"


@dataclass(frozen=True)
class InstrumentView:
    id: int
    symbol: str
    exchange: str
    name: str
    kind: str
    currency: str | None
    isin: str | None
    entity_id: int | None
    entity_name: str | None
    is_active: bool
    last_fetched_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    price: PriceView | None = None

    @property
    def code(self) -> str:
        return f"{self.symbol}.{self.exchange}"

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def exchange_label(self) -> str:
        return EXCHANGE_LABELS.get(self.exchange, self.exchange)

    @property
    def failing(self) -> bool:
        """Its latest fetch failed: an error newer than the last success."""
        return self.last_error_at is not None and (self.last_fetched_at is None or self.last_error_at > self.last_fetched_at)


@dataclass(frozen=True)
class InstrumentDetail:
    instrument: InstrumentView
    prices: list[PriceView] = field(default_factory=list)  # the latest RECENT_LIMIT, the latest first
    price_count: int = 0


@dataclass(frozen=True)
class InstrumentOverview:
    instruments: list[InstrumentView]
    configured: bool  # the source's key is set: the worker fetches
    key_variable: str = eodhd.KEY_VARIABLE


@dataclass(frozen=True)
class InstrumentValues:
    symbol: str
    exchange: str
    name: str
    kind: str
    currency: str | None
    isin: str | None
    entity: str  # as the form names it: "Name (Nr. 12)", a number, a name; empty: by the ISIN

    def row(self, entity_id: int | None) -> dict:
        return {"symbol": self.symbol, "exchange": self.exchange, "name": self.name, "kind": self.kind, "currency": self.currency,
                "isin": self.isin, "entity_id": entity_id}


def parse(form: Mapping[str, str]) -> InstrumentValues:
    """The form's entries, checked. InvalidInstrument names the first that is not."""
    symbol = form.get("symbol", "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise InvalidInstrument("Symbol: Buchstaben, Ziffern, Punkt, Bindestrich, höchstens 30 Zeichen, z. B. NESN oder GSPC.")
    exchange = form.get("exchange", "").strip().upper()
    if not _EXCHANGE.fullmatch(exchange):
        raise InvalidInstrument("Börse: der Code der Kursquelle, z. B. SW, US, XETRA oder INDX für einen Index.")
    name = " ".join(form.get("name", "").split())
    if not name or len(name) > MAX_NAME_CHARS:
        raise InvalidInstrument(f"Name: bitte angeben, höchstens {MAX_NAME_CHARS} Zeichen.")
    kind = form.get("kind", "")
    if kind not in KIND_LABELS:
        raise InvalidInstrument("Art: bitte Aktie, ETF oder Index wählen.")
    currency = form.get("currency", "").strip().upper() or None
    if kind == InstrumentKind.INDEX:
        if currency:
            raise InvalidInstrument("Währung: ein Index zählt in Punkten, ohne Währung.")
    elif currency is None or not _CURRENCY.fullmatch(currency):
        raise InvalidInstrument("Währung: drei Buchstaben nach ISO 4217, z. B. CHF, USD oder EUR.")
    raw_isin = form.get("isin", "").strip()
    isin = identifier_key(AliasScheme.ISIN, raw_isin) if raw_isin else None
    if raw_isin and isin is None:
        raise InvalidInstrument("ISIN: zwölf Zeichen mit gültiger Prüfziffer, z. B. CH0038863350.")
    return InstrumentValues(symbol, exchange, name, kind, currency, isin, form.get("entity", "").strip())


def overview(engine: Engine) -> InstrumentOverview:
    with engine.connect() as connection:
        rows = instrument_repository.listing(connection)
    return InstrumentOverview([_view(row) for row in rows], eodhd.configured())


def detail(engine: Engine, instrument_id: int) -> InstrumentDetail | None:
    with engine.connect() as connection:
        row = instrument_repository.get(connection, instrument_id)
        if row is None:
            return None
        prices = price_repository.recent(connection, instrument_id, RECENT_LIMIT)
        count = price_repository.count(connection, instrument_id)
    return InstrumentDetail(_view(row), [_price(price, row.currency, row.exchange) for price in prices], count)


def of_entities(engine: Engine, entity_ids: Iterable[int]) -> list[InstrumentView]:
    """The active Wertpapiere of these entities with their latest price: an entity's page, an event's."""
    entity_ids = list(entity_ids)
    if not entity_ids:
        return []
    with engine.connect() as connection:
        return [_view(row) for row in instrument_repository.of_entities(connection, entity_ids)]


def create(engine: Engine, form: Mapping[str, str]) -> InstrumentView:
    values = parse(form)
    with engine.begin() as connection:
        if instrument_repository.find(connection, values.symbol, values.exchange) is not None:
            raise InvalidInstrument(f"{values.symbol}.{values.exchange} ist bereits erfasst.")
        instrument_id = instrument_repository.insert_instrument(connection, values.row(_entity_id(connection, values)))
    logger.info("Instrument created", extra={"instrument_id": instrument_id})
    return detail(engine, instrument_id).instrument


def update(engine: Engine, instrument_id: int, form: Mapping[str, str]) -> InstrumentView | None:
    values = parse(form)
    with engine.begin() as connection:
        found = instrument_repository.find(connection, values.symbol, values.exchange)
        if found is not None and found != instrument_id:
            raise InvalidInstrument(f"{values.symbol}.{values.exchange} ist bereits erfasst.")
        if not instrument_repository.update_instrument(connection, instrument_id, values.row(_entity_id(connection, values))):
            return None
    view = detail(engine, instrument_id)
    return view.instrument if view else None


def set_active(engine: Engine, instrument_ids: Iterable[int], active: bool) -> BulkResult:
    return _bulk(engine, instrument_ids, lambda connection, ids, _rows: instrument_repository.set_active(connection, ids, active))


def delete(engine: Engine, instrument_ids: Iterable[int]) -> BulkResult:
    """Delete Wertpapiere without Kurspunkte - all or none: one with Kurspunkte is deactivated instead, its prices stay
    (datamodel.md, rule 14)."""

    def blocker(row: Row) -> str | None:
        return f"{row.name} (hat Kurspunkte; deaktivieren statt löschen)" if row.has_prices else None

    return _bulk(engine, instrument_ids, lambda connection, ids, _rows: instrument_repository.delete_instruments(connection, ids), blocker)


def _bulk(engine: Engine, instrument_ids: Iterable[int], action, blocker=None) -> BulkResult:
    """All or nothing (requirements.md, "Destructive and Bulk Actions"): one not found, or blocked, blocks all."""
    instrument_ids = sorted(set(instrument_ids))
    if not instrument_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        found = {row.id: row for row in instrument_repository.lock_instruments(connection, instrument_ids)}
        blocked = [f"Wertpapier #{each} (nicht gefunden)" for each in instrument_ids if each not in found]
        if blocker:
            blocked += [reason for row in found.values() if (reason := blocker(row))]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        action(connection, instrument_ids, found)
    return BulkResult(processed=len(instrument_ids), blocked=[])


def _entity_id(connection: Connection, values: InstrumentValues) -> int | None:
    """The entity the form names; else the one with the ISIN among its identifiers; else none."""
    text = values.entity
    if text:
        if text.endswith(")") and "(Nr. " in text:
            text = text.rsplit("(Nr. ", 1)[1][:-1]
        if text.isdigit():
            entity = entity_repository.get(connection, int(text))
            if entity is None or entity.merged_into_id is not None:
                raise InvalidInstrument(f"Entität {text} nicht gefunden.")
            return entity.id
        found = entity_repository.live_by_name(connection, text)
        if len(found) != 1:
            raise InvalidInstrument(f"Entität „{text}“: {'nicht eindeutig' if found else 'nicht gefunden'}; bitte aus der Liste wählen.")
        return found[0]
    if values.isin:
        alias = entity_repository.find_identifier(connection, AliasScheme.ISIN, values.isin)
        return alias.entity_id if alias else None
    return None


def _price(row: Row, currency: str | None, exchange: str) -> PriceView:
    return PriceView(row.close, row.previous_close, row.at, row.received_at, row.source, row.delayed, currency, exchange)


def _view(row: Row) -> InstrumentView:
    price = None
    if row.price is not None:
        price = PriceView(row.price, row.previous_close, row.price_at, row.received_at, row.price_source, row.delayed, row.currency,
                          row.exchange)
    return InstrumentView(
        row.id, row.symbol, row.exchange, row.name, row.kind, row.currency, row.isin, row.entity_id, row.entity_name, row.is_active,
        row.last_fetched_at, row.last_error, row.last_error_at, price,
    )
