"""Wertpapiere pages in the web layer, without a database: the services are replaced."""
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.services import (
    entity_correction_service,
    entity_service,
    event_correction_service,
    event_service,
    instrument_service,
    relationship_service,
)
from app.services.bulk import BulkResult
from app.services.entity_service import EntityDetail, EventEntity
from app.services.event_service import EventDetail
from app.services.history_service import KnownBar, MarketStatus
from app.services.instrument_service import (
    ActionView,
    Coverage,
    InstrumentDetail,
    InstrumentOverview,
    InstrumentView,
    InvalidInstrument,
    PriceView,
    SourceStatus,
)
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, SETTINGS, tag_of, text_of
from tests.unit.test_web_entities import entity
from tests.unit.test_web_events import event

AT = datetime(2026, 10, 30, 20, 29, tzinfo=UTC)
SOURCES = [SourceStatus("alpaca", "Alpaca", True, ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")), SourceStatus("six", "SIX", True, ()),
           SourceStatus("eodhd", "EODHD", True, ("EODHD_API_KEY",))]
MARKET = MarketStatus(True, ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"), date(2016, 1, 1), AT, date(2026, 10, 30),
                      date(2026, 10, 30), 11000, 4200, True)


def price(close="79.5", **fields):
    values = dict(close=Decimal(close), previous_close=Decimal("78"), at=AT, received_at=AT, source="six", delayed=True,
                  currency="CHF", exchange="SIX")
    return PriceView(**values | fields)


def instrument(instrument_id, **fields):
    values = dict(id=instrument_id, source="six", source_ref="CH0038863350CHF4", symbol="NESN", exchange="SIX",
                  name=f"Nestlé <{instrument_id}>", kind="stock", currency="CHF", isin=None, entity_id=None, entity_name=None,
                  entity_uncertain=False, live=True, status="active", delisted_on=None, history=False, history_through=None,
                  last_fetched_at=AT, last_error=None, last_error_at=None, price=price())
    return InstrumentView(**values | fields)


def overview(instruments, total=None, sources=SOURCES, market=MARKET):
    return InstrumentOverview(instruments, len(instruments) if total is None else total, sources, market)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def pages(monkeypatch):
    calls = {"overview": overview([instrument(1)]), "filters": [], "details": []}

    def listing(_engine, live_only, source, search):
        calls["filters"].append((live_only, source, search))
        return calls["overview"]

    def detail(_engine, instrument_id, as_of=None):
        calls["details"].append(as_of)
        return calls.get("detail")

    monkeypatch.setattr(instrument_service, "overview", listing)
    monkeypatch.setattr(instrument_service, "detail", detail)
    monkeypatch.setattr(entity_service, "choices", lambda _engine: [])
    return calls


def test_the_list_shows_each_price_with_its_time_exchange_source_and_currency(client, pages):
    pages["overview"] = overview([
        instrument(1, entity_id=7, entity_name="Nestlé"),
        instrument(2, symbol="SMI", name="SMI", kind="index", currency=None, price=price("12001.5", currency=None)),
        instrument(3, price=None, last_error="Kein Kurs von SIX", last_error_at=datetime(2026, 10, 31, tzinfo=UTC)),
        instrument(4, live=False),
        instrument(5, source="alpaca", symbol="TWTR", exchange="NYSE", status="inactive", delisted_on=date(2022, 10, 28),
                   entity_id=8, entity_name="Twitter", entity_uncertain=True, live=False, price=None),
    ])

    html = client.get("/instruments").text

    cell = re.search(r'data-testid="instrument-price-1">(.*?)</span></span>', html, re.DOTALL).group(1)
    assert "<strong>79,50 CHF</strong>" in cell and "+1,92 %" in cell
    assert re.search(r'<time datetime="2026-10-30T20:29:00\+00:00" data-local-time>', cell)
    assert "SIX Swiss Exchange · SIX, verzögert, 15 Minuten" in cell
    assert "<strong>12 001,50 Punkte</strong>" in html
    assert text_of(html, "instrument-price-3") == "Noch kein Kurs"
    status = re.search(r'data-testid="instrument-status-3">(.*?)</td>', html).group(1)
    assert "Fehler" in status and "Kein Kurs von SIX" in status
    assert "Aus" in re.search(r'data-testid="instrument-status-4">(.*?)</td>', html).group(1)
    row = re.search(r'data-testid="instrument-5">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "TWTR · New York Stock Exchange · Alpaca · nicht mehr gehandelt seit 28.10.2022" in row
    assert text_of(html, "entity-uncertain-5") == "unsicher"
    assert tag_of(html, "key-missing-alpaca") is None
    assert 'data-confirm="Wertpapier|Wertpapiere"' in tag_of(html, "delete")
    assert 'aria-current="page"' in tag_of(html, "nav-instruments")
    progress = re.search(r'data-testid="history-progress">(.*?)</p>', html, re.DOTALL).group(1)
    assert "Tageskurse: 4200 von 11000 Wertpapieren bis 30.10.2026" in progress and "Läuft" in progress


def test_the_list_is_filtered_and_says_how_many_it_shows(client, pages):
    pages["overview"] = overview([instrument(1)], total=11000)

    html = client.get("/instruments?source=alpaca&q=aapl&live=1").text

    assert pages["filters"] == [(True, "alpaca", "aapl")]
    assert text_of(html, "instrument-count") == "Angezeigt: die ersten 1 von 11000 Wertpapieren nach Name."
    assert "selected" in re.search(r'<option value="alpaca"[^>]*>', html).group(0)
    assert "checked" in tag_of(html, "filter-live")
    # The filters go along with a bulk action, to come back to the same list.
    assert re.search(r'<input type="hidden" name="back" value="live=1&amp;source=alpaca&amp;q=aapl">', html)


def test_without_the_keys_the_page_names_what_is_not_fetched(client, pages):
    sources = [SourceStatus("alpaca", "Alpaca", False, ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")), SOURCES[1],
               SourceStatus("eodhd", "EODHD", False, ("EODHD_API_KEY",))]
    pages["overview"] = overview([], sources=sources, market=MarketStatus(False, (), date(2025, 1, 1), None, None, None, 0, 0, False))

    html = client.get("/instruments").text

    alpaca = re.search(r'data-testid="key-missing-alpaca">(.*?)</p>', html, re.DOTALL).group(1)
    assert "ALPACA_API_KEY_ID" in alpaca and "ALPACA_API_SECRET_KEY" in alpaca and "Tageskurse" in alpaca
    assert "EODHD_API_KEY" in re.search(r'data-testid="key-missing-eodhd">(.*?)</p>', html, re.DOTALL).group(1)
    assert tag_of(html, "history-progress") is None
    assert text_of(html, "no-instruments").startswith("Noch keine Wertpapiere.")


def test_entering_a_wertpapier_leads_to_its_page_or_says_what_is_wrong(client, pages, monkeypatch):
    def create(_engine, form):
        if form["symbol"] == "?":
            raise InvalidInstrument("Symbol: Buchstaben, Ziffern.")
        return instrument(9)

    monkeypatch.setattr(instrument_service, "create", create)

    response = client.post("/instruments", data={"source": "six", "symbol": "NESN", "valor_id": "CH0038863350CHF4", "name": "Nestlé"})
    assert response.headers["location"] == "/instruments/9"
    assert text_of(client.get("/instruments").text, "flash") == "Nestlé &lt;9&gt; (NESN) erfasst."

    response = client.post("/instruments", data={"symbol": "?"})
    assert response.headers["location"] == "/instruments#new"
    assert text_of(client.get("/instruments").text, "flash") == "Nicht erfasst: Symbol: Buchstaben, Ziffern."


def test_the_page_of_a_wertpapier_shows_its_latest_prices_and_its_entries(client, pages):
    pages["detail"] = InstrumentDetail(instrument(1, isin="CH0038863350", entity_id=7, entity_name="Nestlé"),
                                       [price(), price("79.4", at=datetime(2026, 10, 30, 20, 14, tzinfo=UTC))], 2)

    html = client.get("/instruments/1").text

    assert text_of(html, "meta-code") == "NESN" and text_of(html, "meta-exchange") == "SIX Swiss Exchange"
    assert re.findall(r'data-testid="price-(\d+)"', html) == ["1", "2"]
    assert 'value="Nestlé (Nr. 7)"' in tag_of(html, "field-entity") and 'value="CH0038863350"' in tag_of(html, "field-isin")
    assert 'value="CH0038863350CHF4"' in tag_of(html, "field-valor-id") and tag_of(html, "field-exchange") is None
    assert tag_of(html, "section-history") is None


def test_a_us_wertpapier_shows_its_daily_prices_as_known_then_its_actions_and_only_its_entity_to_change(client, pages):
    us = instrument(1, source="alpaca", source_ref="id-aapl", symbol="AAPL", exchange="NASDAQ", name="Apple Inc.", history=True,
                    entity_id=7, entity_name="Apple", entity_uncertain=True, price=None)
    bars = [KnownBar(date(2024, 6, 10), Decimal(121), Decimal(122), Decimal(118), Decimal("121.8"), 300, Decimal(1)),
            KnownBar(date(2024, 6, 7), Decimal(1200), Decimal(1220), Decimal(1180), Decimal("1208.9"), 40, Decimal("0.1"))]
    actions = [ActionView("forward_split", date(2024, 6, 10), None, date(2024, 6, 7), Decimal(1), Decimal(10), None, None, None, None)]
    as_of = instrument_service.parse_as_of("2024-06-10")
    pages["detail"] = InstrumentDetail(us, [], 0, bars, as_of,
                                       Coverage(date(2016, 1, 4), date(2024, 6, 10), 2100, 2102, [date(2024, 5, 27)]), actions)

    html = client.get("/instruments/1?as_of=2024-06-10").text

    assert pages["details"] == [as_of]
    assert 'value="2024-06-10"' in tag_of(html, "field-as-of")
    assert text_of(html, "coverage") == "2100 Handelstage vom 04.01.2016 bis 10.06.2024, 2 ohne Kurs, zuletzt 27.05.2024."
    assert re.findall(r'data-testid="bar-(\d+)"', html) == ["1", "2"]
    assert text_of(html, "bar-adjusted-1") == "121,80"
    assert re.sub(r"\s+", " ", re.search(r'data-testid="bar-adjusted-2">(.*?)</td>', html, re.DOTALL).group(1)) == (
        '120,89 <span class="hint">× 0,1000</span>'
    )
    action = re.search(r'data-testid="action-1">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "Aktiensplit" in action and "1 : 10" in action and "07.06.2024" in action
    assert "unsicher" in re.search(r'data-testid="meta-entity">(.*?)</td>', html, re.DOTALL).group(1)
    # From Alpaca's list: only the entity is entered here.
    assert tag_of(html, "field-entity") is not None and tag_of(html, "field-symbol") is None and tag_of(html, "field-name") is None


def test_an_unknown_wertpapier_goes_back_to_the_list(client, pages):
    response = client.get("/instruments/99")

    assert (response.status_code, response.headers["location"]) == (303, "/instruments")


@pytest.mark.parametrize(("path", "service", "message"), [
    ("live-on", "set_live", "1 Wertpapier mit Live-Kurs."),
    ("live-off", "set_live", "1 Wertpapier ohne Live-Kurs."),
    ("delete", "delete", "1 Wertpapier gelöscht."),
])
def test_bulk_actions_pass_the_selection_report_the_result_and_return_to_the_list_as_filtered(
    client, pages, monkeypatch, path, service, message
):
    calls = []
    monkeypatch.setattr(instrument_service, service, lambda _engine, ids, *rest: calls.append((ids, *rest)) or BulkResult(1, []))

    response = client.post(f"/instruments/{path}", data={"instrument_id": ["3"], "back": "live=&source=alpaca&q=aapl&evil=1"})

    assert calls[0][0] == [3] and response.headers["location"] == "/instruments?source=alpaca&q=aapl"
    if service == "set_live":
        assert calls[0][1] is (path == "live-on")
    assert text_of(client.get("/instruments").text, "flash") == message


def test_a_wertpapier_with_prices_is_not_deleted(client, pages, monkeypatch):
    blocked = "Nestlé (hat Kurspunkte; Live-Kurs ausschalten statt löschen)"
    monkeypatch.setattr(instrument_service, "delete", lambda _engine, ids: BulkResult(0, [blocked]))

    response = client.post("/instruments/delete", data={"instrument_id": ["3"]})

    assert response.headers["location"] == "/instruments"
    assert text_of(client.get("/instruments").text, "flash") == f"Nichts gelöscht, blockiert: {blocked}."


def test_an_entity_and_an_event_show_the_latest_prices_of_their_wertpapiere(client, monkeypatch):
    monkeypatch.setattr(instrument_service, "of_entities", lambda _engine, entity_ids: [instrument(1, entity_id=7, entity_name="Nestlé")])
    monkeypatch.setattr(entity_service, "choices", lambda _engine: [])
    monkeypatch.setattr(entity_service, "detail", lambda _engine, entity_id: EntityDetail(entity(7), [], [], 0))
    monkeypatch.setattr(entity_correction_service, "similar_entities", lambda _engine, entity_id: [])
    monkeypatch.setattr(relationship_service, "of_entity", lambda _engine, entity_id: [])
    monkeypatch.setattr(entity_service, "of_event", lambda _engine, event_id: [EventEntity(7, "company", "Nestlé", 1, False, ["Nestlé"])])
    monkeypatch.setattr(event_service, "detail", lambda _engine, event_id: EventDetail(event(8)))
    monkeypatch.setattr(event_correction_service, "similar_events", lambda _engine, event_id: [])

    for path in ("/entities/7", "/events/8"):
        html = client.get(path).text
        section = re.search(r'data-testid="instruments-section">(.*?)</details>', html, re.DOTALL).group(1)
        assert "<strong>79,50 CHF</strong>" in section and 'href="/instruments/1"' in section

