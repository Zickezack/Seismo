"""Wertpapiere (userstories.md, Live-Kurse und Kursdatenspeicher, story 1; Historische Markt- und Ereignisdaten,
story 1): the stocks, ETFs and indices Seismo keeps prices of. Alpaca's list brings every US stock and ETF with its
daily prices (history_service); Swiss ones from SIX and others from EODHD are entered by the admin or imported with the
starter list. Any of them can have a Live-Kurs, fetched at the interval of the Plattform-Einstellungen (price_service).
Each is linked to the Entität it belongs to - by its ISIN, its ticker or its name, the last uncertain -, or chosen.
Every price is shown with its time, source, exchange, currency and delay (requirements.md, Live-Kurse und
Kursdatenspeicher, criterion 1).
"""
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import Connection, Engine, Row

from app.db.models import AliasScheme, GapCause, InstrumentKind, InstrumentStatus, PriceResolution, PriceSource
from app.prices import alpaca, eodhd, six
from app.repositories import entity_repository, instrument_repository, market_data_repository, price_repository
from app.services import history_service, platform_settings_service, price_service, tracking_service
from app.services.bulk import BulkResult
from app.services.entity_names import identifier_key
from app.services.history_service import KnownBar

logger = logging.getLogger(__name__)

KIND_LABELS = {InstrumentKind.STOCK: "Aktie", InstrumentKind.ETF: "ETF", InstrumentKind.INDEX: "Index"}
SOURCE_LABELS = {alpaca.NAME: alpaca.LABEL, six.NAME: six.LABEL, eodhd.NAME: eodhd.LABEL}
DELAY_LABELS = {alpaca.NAME: alpaca.DELAY_LABEL, six.NAME: six.DELAY_LABEL, eodhd.NAME: eodhd.DELAY_LABEL}
# The sources the admin enters Wertpapiere of; Alpaca's come with its list.
ENTERED_SOURCES = {six.NAME: six.LABEL, eodhd.NAME: eodhd.LABEL}
# The exchange codes of the price sources a page names in words; any other as it is.
EXCHANGE_LABELS = {
    "NASDAQ": "Nasdaq", "NYSE": "New York Stock Exchange", "ARCA": "NYSE Arca", "AMEX": "NYSE American", "BATS": "Cboe BZX",
    "SIX": "SIX Swiss Exchange",
    "US": "US-Börsen", "SW": "SIX Swiss Exchange", "XETRA": "Xetra", "F": "Börse Frankfurt", "LSE": "London Stock Exchange",
    "PA": "Euronext Paris", "AS": "Euronext Amsterdam", "MI": "Borsa Italiana", "MC": "Bolsa de Madrid", "VI": "Wiener Börse",
    "TO": "Toronto Stock Exchange", "INDX": "Index",
}
ACTION_LABELS = {
    "forward_split": "Aktiensplit", "reverse_split": "Zusammenlegung", "unit_split": "Aufteilung", "cash_dividend": "Dividende",
    "stock_dividend": "Stockdividende", "spin_off": "Abspaltung", "cash_merger": "Übernahme (bar)",
    "stock_merger": "Übernahme (Aktien)", "stock_and_cash_merger": "Übernahme (bar und Aktien)",
    "name_change": "Symbolwechsel", "worthless_removal": "Wertlos ausgebucht",
}
LIST_LIMIT = 200
RECENT_LIMIT = 100
BARS_LIMIT = 60
GAPS_LIMIT = 10
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-_^]{0,29}")
_EXCHANGE = re.compile(r"[A-Z0-9]{1,20}")
_CURRENCY = re.compile(r"[A-Z]{3}")
# SIX's ValorId: ISIN, currency, market number - CH0038863350CHF4.
_VALOR_ID = re.compile(r"([A-Z]{2}[A-Z0-9]{9}[0-9])([A-Z]{3})([0-9]{1,3})")
MAX_NAME_CHARS = 200
SIX_EXCHANGE = "SIX"


class InvalidInstrument(ValueError):
    """Invalid entries from the Wertpapier form. The message is shown to the admin as is."""


def number_label(value: Decimal | None) -> str:
    """A price as the pages show it: a thin space between thousands, a decimal comma, two places - four below one."""
    if value is None:
        return "–"
    places = 4 if abs(value) < 1 else 2
    return f"{value:,.{places}f}".replace(",", " ").replace(".", ",")


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
    source: str
    source_ref: str
    symbol: str
    exchange: str
    name: str
    kind: str
    currency: str | None
    isin: str | None
    entity_id: int | None
    entity_name: str | None
    entity_uncertain: bool
    live: bool
    status: str
    delisted_on: date | None
    history: bool
    history_through: date | None
    last_fetched_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    price: PriceView | None = None
    tracked: bool = False  # its prices collected continuously (tracking_service, UI: verfolgt)
    tracked_since: datetime | None = None
    collected_through: datetime | None = None

    @property
    def code(self) -> str:
        """As its source names it: EODHD's code ("NESN.SW"), else the symbol."""
        return f"{self.symbol}.{self.exchange}" if self.source == PriceSource.EODHD else self.symbol

    @property
    def kind_label(self) -> str:
        return KIND_LABELS[self.kind]

    @property
    def exchange_label(self) -> str:
        return EXCHANGE_LABELS.get(self.exchange, self.exchange)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def listed(self) -> bool:
        """It comes with its source's list (Alpaca): the list keeps its entries, the admin only links and switches."""
        return self.source == PriceSource.ALPACA

    @property
    def traded(self) -> bool:
        return self.status == InstrumentStatus.ACTIVE

    @property
    def failing(self) -> bool:
        """Its latest fetch failed: an error newer than the last success."""
        return self.last_error_at is not None and (self.last_fetched_at is None or self.last_error_at > self.last_fetched_at)


@dataclass(frozen=True)
class ActionView:
    kind: str
    ex_date: date | None
    effective_date: date | None
    process_date: date | None
    old_rate: Decimal | None
    new_rate: Decimal | None
    rate: Decimal | None
    old_symbol: str | None
    new_symbol: str | None
    currency: str | None

    @property
    def day(self) -> date | None:
        return self.ex_date or self.effective_date or self.process_date

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.kind, self.kind)

    @property
    def summary(self) -> str:
        if self.kind in history_service.SPLITS and self.old_rate and self.new_rate:
            return f"{_ratio(self.old_rate)} : {_ratio(self.new_rate)}"
        if self.kind == "name_change":
            return f"{self.old_symbol or '?'} → {self.new_symbol or '?'}"
        if self.kind == "cash_dividend" and self.rate is not None:
            return f"{number_label(Decimal(self.rate))} {self.currency or ''}".strip()
        if self.rate is not None:
            return f"Verhältnis {_ratio(self.rate)}"
        return ""


@dataclass(frozen=True)
class Coverage:
    first_day: date | None
    last_day: date | None
    bars: int
    trading_days: int
    gaps: list[date] = field(default_factory=list)  # the latest GAPS_LIMIT

    @property
    def missing(self) -> int:
        return max(self.trading_days - self.bars, 0)


GAP_CAUSE_LABELS = {GapCause.NOT_ASKED: "nicht abgefragt (Worker oder Warteschlange)", GapCause.SOURCE_ERROR: "Quelle fehlerhaft"}
RESOLUTION_LABELS = {
    PriceResolution.QUOTE: "Einzelkurse", PriceResolution.MINUTE: "Minuten", PriceResolution.HOUR: "Stunden", PriceResolution.DAY: "Tage",
}
COLLECTION_LABELS = {
    PriceSource.ALPACA: "Minutenbalken aller US-Börsen (SIP), ab 15 Minuten alt, alle 15 Minuten nachgeholt",
    PriceSource.SIX: "der letzte Kurs jede Minute, 15 Minuten verzögert; Handelszeit Mo–Fr 9:00–17:30 Zürich",
    PriceSource.EODHD: "der letzte Kurs jede Minute, verzögert; jede Stunde zählt als Handelszeit",
}
MINUTES_LIMIT = 60
COLLECTION_GAPS_LIMIT = 20


@dataclass(frozen=True)
class GapView:
    starts_at: datetime
    ends_at: datetime | None  # None: still open, nothing collected since
    cause: str | None

    @property
    def cause_label(self) -> str:
        return GAP_CAUSE_LABELS[self.cause] if self.cause else "seither nichts erfasst"

    @property
    def duration_label(self) -> str:
        minutes = int(((self.ends_at or datetime.now(UTC)) - self.starts_at).total_seconds() // 60)
        return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


@dataclass(frozen=True)
class Collection:
    """How a Wertpapier's prices are collected and kept (Live-Kurse und Kursdatenspeicher, story 2)."""

    source: str
    counts: dict[str, int]  # its Kurspunkte by resolution
    gaps: list[GapView]  # the latest COLLECTION_GAPS_LIMIT, an open one first
    gap_count: int
    full_resolution_days: int  # 0: unlimited
    minutes: list[PriceView] = field(default_factory=list)  # Alpaca's latest MINUTES_LIMIT, the latest first
    backfill_through: datetime | None = None  # Alpaca's minutes from before tracking began are in up to here
    tracked_since: datetime | None = None

    @property
    def backfilling(self) -> bool:
        """Its minutes from before tracking began are still being fetched."""
        return self.backfill_through is not None and self.tracked_since is not None and self.backfill_through < self.tracked_since

    @property
    def label(self) -> str:
        return COLLECTION_LABELS[self.source]

    @property
    def counts_label(self) -> str:
        parts = [f"{self.counts[key]} {label}" for key, label in RESOLUTION_LABELS.items() if self.counts.get(key)]
        return ", ".join(parts) or "noch keine"


@dataclass(frozen=True)
class InstrumentDetail:
    instrument: InstrumentView
    prices: list[PriceView] = field(default_factory=list)  # the latest RECENT_LIMIT quotes, the latest first
    price_count: int = 0
    bars: list[KnownBar] = field(default_factory=list)  # as known at `as_of`, the latest first
    as_of: datetime | None = None  # None: now
    coverage: Coverage | None = None
    actions: list[ActionView] = field(default_factory=list)
    collection: Collection | None = None  # once it was tracked


@dataclass(frozen=True)
class SourceStatus:
    name: str
    label: str
    configured: bool
    key_variables: tuple[str, ...]


@dataclass(frozen=True)
class InstrumentOverview:
    instruments: list[InstrumentView]  # at most LIST_LIMIT, by name
    total: int  # matching the filters
    sources: list[SourceStatus]
    market: history_service.MarketStatus


@dataclass(frozen=True)
class InstrumentValues:
    source: str
    source_ref: str
    symbol: str
    exchange: str
    name: str
    kind: str
    currency: str | None
    isin: str | None
    entity: str  # as the form names it: "Name (Nr. 12)", a number, a name; empty: by the ISIN

    def row(self, entity_id: int | None) -> dict:
        return {"source": self.source, "source_ref": self.source_ref, "symbol": self.symbol, "exchange": self.exchange, "name": self.name,
                "kind": self.kind, "currency": self.currency, "isin": self.isin, "entity_id": entity_id, "entity_uncertain": False}


def parse(form: Mapping[str, str], source: str | None = None) -> InstrumentValues:
    """The form's entries, checked; `source` that of the Wertpapier changed, else the form's. InvalidInstrument names
    the first that is not."""
    source = source or form.get("source", "")
    if source not in ENTERED_SOURCES:
        raise InvalidInstrument("Quelle: bitte SIX oder EODHD wählen; US-Wertpapiere kommen mit der Liste von Alpaca.")
    symbol = form.get("symbol", "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise InvalidInstrument("Symbol: Buchstaben, Ziffern, Punkt, Bindestrich, höchstens 30 Zeichen, z. B. NESN oder SMI.")
    raw_isin = form.get("isin", "").strip()
    isin = identifier_key(AliasScheme.ISIN, raw_isin) if raw_isin else None
    if raw_isin and isin is None:
        raise InvalidInstrument("ISIN: zwölf Zeichen mit gültiger Prüfziffer, z. B. CH0038863350.")
    if source == six.NAME:
        valor = _VALOR_ID.fullmatch(form.get("valor_id", "").strip().upper())
        if valor is None or identifier_key(AliasScheme.ISIN, valor.group(1)) is None:
            raise InvalidInstrument("ValorId: ISIN, Währung und Markt wie SIX sie nennt, z. B. CH0038863350CHF4.")
        source_ref, exchange, isin = valor.group(0), SIX_EXCHANGE, isin or valor.group(1)
    else:
        exchange = form.get("exchange", "").strip().upper()
        if not _EXCHANGE.fullmatch(exchange):
            raise InvalidInstrument("Börse: der Code von EODHD, z. B. XETRA, LSE oder INDX für einen Index.")
        source_ref = f"{symbol}.{exchange}"
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
    return InstrumentValues(source, source_ref, symbol, exchange, name, kind, currency, isin, form.get("entity", "").strip())


def parse_as_of(raw: str | None) -> datetime | None:
    """A day of the form as the end of that day in New York - what was known after its close -, None for none or an
    unreadable one."""
    try:
        day = date.fromisoformat((raw or "").strip())
    except ValueError:
        return None
    return datetime.combine(day, time(23, 59, 59), alpaca.NEW_YORK).astimezone(UTC)


def overview(
    engine: Engine, live_only: bool = False, source: str | None = None, search: str | None = None, tracked_only: bool = False
) -> InstrumentOverview:
    """The Wertpapiere by name; an unknown source filters nothing."""
    source = source if source in SOURCE_LABELS else None
    search = (search or "").strip() or None
    with engine.connect() as connection:
        rows = instrument_repository.listing(connection, live_only, source, search, LIST_LIMIT, tracked_only)
        total = instrument_repository.count_listing(connection, live_only, source, search, tracked_only)
        market = history_service.status(connection)
    sources = [
        SourceStatus(alpaca.NAME, alpaca.LABEL, price_service.configured(alpaca.NAME), alpaca.KEY_VARIABLES),
        SourceStatus(six.NAME, six.LABEL, True, ()),
        SourceStatus(eodhd.NAME, eodhd.LABEL, price_service.configured(eodhd.NAME), (eodhd.KEY_VARIABLE,)),
    ]
    return InstrumentOverview([_view(row) for row in rows], total, sources, market)


def detail(engine: Engine, instrument_id: int, as_of: datetime | None = None) -> InstrumentDetail | None:
    """The Wertpapier with its latest quotes, its daily prices as known at `as_of` (None: now), their coverage, and
    its corporate actions."""
    with engine.connect() as connection:
        row = instrument_repository.get(connection, instrument_id)
        if row is None:
            return None
        prices = price_repository.recent(connection, instrument_id, RECENT_LIMIT)
        count = price_repository.count(connection, instrument_id, PriceResolution.QUOTE)
        coverage, bars, actions = None, [], []
        if row.history:
            bars = history_service.known_on(connection, instrument_id, as_of or datetime.now(UTC), BARS_LIMIT)
            span = market_data_repository.coverage(connection, instrument_id)
            gaps = []
            if span.bars:
                gaps = market_data_repository.gaps(connection, instrument_id, span.first_day, span.last_day, GAPS_LIMIT)
            coverage = Coverage(span.first_day, span.last_day, span.bars, span.trading_days or 0, gaps)
            actions = [_action(each) for each in market_data_repository.actions(connection, instrument_id)]
        collection = _collection(connection, row) if row.tracked_since is not None else None
    return InstrumentDetail(
        _view(row), [_price(price, row.currency, row.exchange) for price in prices], count, bars, as_of, coverage, actions, collection,
    )


def _collection(connection: Connection, row: Row, now: datetime | None = None) -> Collection:
    """Its points by resolution, its gaps - one still open first, while it is tracked -, Alpaca's latest minutes."""
    now = now or datetime.now(UTC)
    gaps = [GapView(gap.starts_at, gap.ends_at, gap.cause) for gap in price_repository.gaps(connection, row.id, COLLECTION_GAPS_LIMIT)]
    if row.tracked and row.collected_through is not None:
        if row.source == PriceSource.ALPACA:
            behind = tracking_service.minutes_end(now) - row.collected_through > tracking_service.GAP_FACTOR * tracking_service.MINUTES_EVERY
        else:
            behind = tracking_service.is_gap(row.source, row.collected_through, now)
        if behind:
            gaps.insert(0, GapView(row.collected_through, None, None))
    minutes = []
    if row.source == PriceSource.ALPACA:
        minutes = [_price(each, row.currency, row.exchange) for each in price_repository.recent(connection, row.id, MINUTES_LIMIT, PriceResolution.MINUTE)]
    return Collection(
        row.source, price_repository.counts_by_resolution(connection, row.id), gaps, price_repository.count_gaps(connection, row.id),
        platform_settings_service.load(connection).price_full_resolution_days, minutes, row.backfill_through, row.tracked_since,
    )


def of_entities(engine: Engine, entity_ids: Iterable[int]) -> list[InstrumentView]:
    """The traded Wertpapiere of these entities with their latest price: an entity's page, an event's."""
    entity_ids = list(entity_ids)
    if not entity_ids:
        return []
    with engine.connect() as connection:
        return [_view(row) for row in instrument_repository.of_entities(connection, entity_ids)]


def create(engine: Engine, form: Mapping[str, str]) -> InstrumentView:
    values = parse(form)
    with engine.begin() as connection:
        if instrument_repository.find(connection, values.source, values.source_ref) is not None:
            raise InvalidInstrument(f"{values.source_ref} ist bei {ENTERED_SOURCES[values.source]} bereits erfasst.")
        instrument_id = instrument_repository.insert_instrument(connection, values.row(_entity_id(connection, values.entity, values.isin)))
    logger.info("Instrument created", extra={"instrument_id": instrument_id})
    return detail(engine, instrument_id).instrument


def update(engine: Engine, instrument_id: int, form: Mapping[str, str]) -> InstrumentView | None:
    """Its entries; of one from Alpaca's list only its Entität - the list keeps the rest."""
    with engine.begin() as connection:
        current = instrument_repository.get(connection, instrument_id)
        if current is None:
            return None
        if current.source == PriceSource.ALPACA:
            values = {"entity_id": _entity_id(connection, form.get("entity", "").strip(), current.isin), "entity_uncertain": False}
        else:
            parsed = parse(form, current.source)
            found = instrument_repository.find(connection, parsed.source, parsed.source_ref)
            if found is not None and found != instrument_id:
                raise InvalidInstrument(f"{parsed.source_ref} ist bei {ENTERED_SOURCES[parsed.source]} bereits erfasst.")
            values = parsed.row(_entity_id(connection, parsed.entity, parsed.isin))
        instrument_repository.update_instrument(connection, instrument_id, values)
    view = detail(engine, instrument_id)
    return view.instrument if view else None


def set_live(engine: Engine, instrument_ids: Iterable[int], live: bool) -> BulkResult:
    return _bulk(engine, instrument_ids, lambda connection, ids, _rows: instrument_repository.set_live(connection, ids, live))


def delete(engine: Engine, instrument_ids: Iterable[int]) -> BulkResult:
    """Delete Wertpapiere without Kurspunkte - all or none: one with Kurspunkte keeps them, its Live-Kurs is switched off
    instead (datamodel.md, rule 14); one from Alpaca's list would come back with it."""

    def blocker(row: Row) -> str | None:
        if row.source == PriceSource.ALPACA:
            return f"{row.name} (aus der Liste von Alpaca; Live-Kurs ausschalten statt löschen)"
        return f"{row.name} (hat Kurspunkte; Live-Kurs ausschalten statt löschen)" if row.has_prices else None

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


def _entity_id(connection: Connection, text: str, isin: str | None) -> int | None:
    """The entity the form names; else the one with the ISIN among its identifiers; else none."""
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
    if isin:
        alias = entity_repository.find_identifier(connection, AliasScheme.ISIN, isin)
        return alias.entity_id if alias else None
    return None


def _ratio(value: Decimal | float) -> str:
    return f"{Decimal(value).normalize():f}".replace(".", ",")


def _action(row: Row) -> ActionView:
    return ActionView(
        row.kind, row.ex_date, row.effective_date, row.process_date, row.old_rate, row.new_rate, row.rate, row.old_symbol,
        row.new_symbol, row.currency,
    )


def _price(row: Row, currency: str | None, exchange: str) -> PriceView:
    return PriceView(row.close, row.previous_close, row.at, row.received_at, row.source, row.delayed, currency, exchange)


def _view(row: Row) -> InstrumentView:
    price = None
    if row.price is not None:
        price = PriceView(row.price, row.previous_close, row.price_at, row.received_at, row.price_source, row.delayed, row.currency,
                          row.exchange)
    return InstrumentView(
        row.id, row.source, row.source_ref, row.symbol, row.exchange, row.name, row.kind, row.currency, row.isin, row.entity_id,
        row.entity_name, row.entity_uncertain, row.live, row.status, row.delisted_on, row.history, row.history_through,
        row.last_fetched_at, row.last_error, row.last_error_at, price,
        tracked=row.tracked, tracked_since=row.tracked_since, collected_through=row.collected_through,
    )
