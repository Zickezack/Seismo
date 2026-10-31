"""Wertpapiere pages in the web layer, without a database: the services are replaced."""
import re
from datetime import UTC, datetime
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
from app.services.entity_service import EntityDetail, EventEntity
from app.services.event_service import EventDetail
from app.services.bulk import BulkResult
from app.services.instrument_service import InstrumentDetail, InstrumentOverview, InstrumentView, InvalidInstrument, PriceView
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, SETTINGS, tag_of, text_of
from tests.unit.test_web_entities import entity
from tests.unit.test_web_events import event

AT = datetime(2026, 10, 30, 20, 29, tzinfo=UTC)


def price(close="79.5", **fields):
    values = dict(close=Decimal(close), previous_close=Decimal("78"), at=AT, received_at=AT, source="eodhd", delayed=True,
                  currency="CHF", exchange="SW")
    return PriceView(**values | fields)


def instrument(instrument_id, **fields):
    values = dict(id=instrument_id, symbol="NESN", exchange="SW", name=f"Nestlé <{instrument_id}>", kind="stock", currency="CHF",
                  isin=None, entity_id=None, entity_name=None, is_active=True, last_fetched_at=AT, last_error=None, last_error_at=None,
                  price=price())
    return InstrumentView(**values | fields)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def pages(monkeypatch):
    calls = {"overview": InstrumentOverview([instrument(1)], configured=True)}
    monkeypatch.setattr(instrument_service, "overview", lambda _engine: calls["overview"])
    monkeypatch.setattr(instrument_service, "detail", lambda _engine, instrument_id: calls.get("detail"))
    monkeypatch.setattr(entity_service, "choices", lambda _engine: [])
    return calls


def test_the_list_shows_each_price_with_its_time_exchange_source_and_currency(client, pages):
    pages["overview"] = InstrumentOverview([
        instrument(1, entity_id=7, entity_name="Nestlé"),
        instrument(2, symbol="GSPC", exchange="INDX", name="S&P 500", kind="index", currency=None,
                   price=price("5998.04", currency=None, exchange="INDX")),
        instrument(3, price=None, last_error="Kein Kurs von EODHD", last_error_at=datetime(2026, 10, 31, tzinfo=UTC)),
        instrument(4, is_active=False),
    ], configured=True)

    html = client.get("/instruments").text

    cell = re.search(r'data-testid="instrument-price-1">(.*?)</span></span>', html, re.DOTALL).group(1)
    assert "<strong>79,50 CHF</strong>" in cell and "+1,92 %" in cell
    assert re.search(r'<time datetime="2026-10-30T20:29:00\+00:00" data-local-time>', cell)
    assert "SIX Swiss Exchange · EODHD, verzögert, 15 bis 20 Minuten" in cell
    assert "<strong>5 998,04 Punkte</strong>" in html and "GSPC.INDX · Index" in html
    assert text_of(html, "instrument-price-3") == "Noch kein Kurs"
    status = re.search(r'data-testid="instrument-status-3">(.*?)</td>', html).group(1)
    assert "Fehler" in status and "Kein Kurs von EODHD" in status
    assert "Inaktiv" in re.search(r'data-testid="instrument-status-4">(.*?)</td>', html).group(1)
    assert tag_of(html, "key-missing") is None
    assert 'data-confirm="Wertpapier|Wertpapiere"' in tag_of(html, "delete")
    assert 'aria-current="page"' in tag_of(html, "nav-instruments")


def test_without_the_key_the_page_says_nothing_is_fetched(client, pages):
    pages["overview"] = InstrumentOverview([], configured=False)

    html = client.get("/instruments").text

    assert "EODHD_API_KEY" in re.search(r'data-testid="key-missing">(.*?)</p>', html, re.DOTALL).group(1)
    assert text_of(html, "no-instruments").startswith("Noch keine Wertpapiere.")


def test_entering_a_wertpapier_leads_to_its_page_or_says_what_is_wrong(client, pages, monkeypatch):
    def create(_engine, form):
        if form["symbol"] == "?":
            raise InvalidInstrument("Symbol: Buchstaben, Ziffern.")
        return instrument(9)

    monkeypatch.setattr(instrument_service, "create", create)

    response = client.post("/instruments", data={"symbol": "NESN", "exchange": "SW", "name": "Nestlé", "kind": "stock", "currency": "CHF"})
    assert response.headers["location"] == "/instruments/9"
    assert text_of(client.get("/instruments").text, "flash") == "Nestlé &lt;9&gt; (NESN.SW) erfasst."

    response = client.post("/instruments", data={"symbol": "?"})
    assert response.headers["location"] == "/instruments#new"
    assert text_of(client.get("/instruments").text, "flash") == "Nicht erfasst: Symbol: Buchstaben, Ziffern."


def test_the_page_of_a_wertpapier_shows_its_latest_prices_and_its_entries(client, pages):
    pages["detail"] = InstrumentDetail(instrument(1, isin="CH0038863350", entity_id=7, entity_name="Nestlé"),
                                       [price(), price("79.4", at=datetime(2026, 10, 30, 20, 14, tzinfo=UTC))], 2)

    html = client.get("/instruments/1").text

    assert text_of(html, "meta-code") == "NESN.SW" and text_of(html, "meta-exchange") == "SIX Swiss Exchange"
    assert re.findall(r'data-testid="price-(\d+)"', html) == ["1", "2"]
    assert 'value="Nestlé (Nr. 7)"' in tag_of(html, "field-entity") and 'value="CH0038863350"' in tag_of(html, "field-isin")


def test_an_unknown_wertpapier_goes_back_to_the_list(client, pages):
    response = client.get("/instruments/99")

    assert (response.status_code, response.headers["location"]) == (303, "/instruments")


@pytest.mark.parametrize(("path", "service", "message"), [
    ("activate", "set_active", "1 Wertpapier aktiviert."),
    ("deactivate", "set_active", "1 Wertpapier deaktiviert."),
    ("delete", "delete", "1 Wertpapier gelöscht."),
])
def test_bulk_actions_pass_the_selection_and_report_the_result(client, pages, monkeypatch, path, service, message):
    calls = []
    monkeypatch.setattr(instrument_service, service, lambda _engine, ids, *rest: calls.append((ids, *rest)) or BulkResult(1, []))

    response = client.post(f"/instruments/{path}", data={"instrument_id": ["3"]})

    assert calls[0][0] == [3] and response.headers["location"] == "/instruments"
    assert text_of(client.get("/instruments").text, "flash") == message


def test_a_wertpapier_with_prices_is_not_deleted(client, pages, monkeypatch):
    monkeypatch.setattr(instrument_service, "delete", lambda _engine, ids: BulkResult(0, ["Nestlé (hat Kurspunkte; deaktivieren statt löschen)"]))

    client.post("/instruments/delete", data={"instrument_id": ["3"]})

    assert text_of(client.get("/instruments").text, "flash") == "Nichts gelöscht, blockiert: Nestlé (hat Kurspunkte; deaktivieren statt löschen)."


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
