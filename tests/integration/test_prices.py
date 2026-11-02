"""Wertpapiere and their live prices in PostgreSQL: entered and imported, linked to entities, and fetched at the
interval from simulated SIX, Alpaca and EODHD, each Wertpapier from its own source (price_service,
instrument_service)."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from sqlalchemy import select, text, update

from app.db.models import AliasMatch, AliasScheme, Instrument, JobStatus, PricePoint
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.repositories import entity_repository, instrument_repository
from app.services import instrument_service, job_service, platform_settings_service, price_service, seed_service
from app.services.instrument_service import InvalidInstrument
from app.sources.http import HttpClient
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_ingestion import jobs

KIND = job_service.FETCH_PRICES_JOB_KIND
NESTLE = {"source": "six", "symbol": "NESN", "valor_id": "CH0038863350CHF4", "name": "Nestlé", "kind": "stock", "currency": "CHF"}
SAP = {"source": "eodhd", "symbol": "SAP", "exchange": "XETRA", "name": "SAP", "kind": "stock", "currency": "EUR"}
SIX_FIELDS = ["ValorId", "ClosingPrice", "PreviousClosingPrice", "OpeningPrice", "DailyHighPrice", "DailyLowPrice", "TotalVolume",
              "LatestTradeDate", "LatestTradeTime"]


class Market:
    """Answers SIX's, Alpaca's and EODHD's live price requests with `prices` (ref: (time, close)), or with `status`."""

    def __init__(self):
        self.prices, self.status, self.requests = {}, 200, []

    def __call__(self, request):
        url = str(request.url)
        self.requests.append((url, dict(request.headers)))
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, text="User-agent: *\nAllow: /fqs/\nDisallow: /api/\n")
        if self.status != 200:
            return httpx2.Response(self.status)
        query = parse_qs(urlsplit(url).query)
        if request.url.host == "www.six-group.com":
            refs = query["where"][0].split("=", 1)[1].split("|")
            rows = [
                [ref, self.prices[ref][1], 79, None, None, None, 1000, int(self.prices[ref][0].strftime("%Y%m%d")),
                 float(self.prices[ref][0].strftime("%H%M%S"))]
                for ref in refs if ref in self.prices
            ]
            return httpx2.Response(200, json={"colNames": SIX_FIELDS, "rowData": rows})
        if request.url.host == "data.alpaca.markets":
            return httpx2.Response(200, json={
                symbol: {"latestTrade": {"t": self.prices[symbol][0].isoformat(), "p": self.prices[symbol][1]}, "prevDailyBar": {"c": 100}}
                for symbol in query["symbols"][0].split(",") if symbol in self.prices
            })
        codes = [request.url.path.rsplit("/", 1)[1], *filter(None, query.get("s", [""])[0].split(","))]
        return httpx2.Response(200, json=[
            {"code": code, "timestamp": self.prices[code][0].timestamp(), "close": self.prices[code][1]} if code in self.prices
            else {"code": code, "timestamp": "NA", "close": "NA"}
            for code in codes
        ])


@pytest.fixture
def market(monkeypatch):
    fake = Market()
    monkeypatch.setenv("EODHD_API_KEY", "eodhd-key")
    monkeypatch.setenv("ALPACA_API_KEY_ID", "alpaca-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "alpaca-secret")
    monkeypatch.setattr(price_service, "HttpClient", lambda contact: HttpClient(contact, httpx2.MockTransport(fake)))
    return fake


def run_fetches(database):
    """Run the queued fetches, the hosts free again first: as a minute later."""
    with database.begin() as connection:
        connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = now() - interval '1 minute'"))
        connection.execute(text("UPDATE job SET run_after = now() WHERE status = 'queued'"))
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    while run_next_job(database, {KIND: handlers[KIND]}, "test", 10, on_renewed=lambda: None):
        pass


def asked_long_ago(database):
    """As if the interval were over."""
    with database.begin() as connection:
        connection.execute(update(Instrument).values(last_fetched_at=text("now() - interval '1 day'"), last_error_at=None))


def prices(database):
    with database.connect() as connection:
        return connection.execute(select(PricePoint.__table__).order_by(PricePoint.id)).all()


def instrument_row(database, instrument_id):
    with database.connect() as connection:
        return connection.execute(select(Instrument.__table__).where(Instrument.id == instrument_id)).one()


def listed(database, symbol, live=True):
    """A Wertpapier as Alpaca's list brings it."""
    with database.begin() as connection:
        return instrument_repository.insert_instrument(connection, {
            "source": "alpaca", "source_ref": f"id-{symbol}", "symbol": symbol, "exchange": "NASDAQ", "name": f"{symbol} Inc.",
            "kind": "stock", "currency": "USD", "live": live, "history": True,
        })


def test_each_source_gets_its_own_batches_and_none_without_its_key(database, market, monkeypatch):
    six = [instrument_service.create(database, NESTLE | {"symbol": f"S{n}", "valor_id": f"CH0038863350CHF{n}"}).id for n in range(21)]
    instrument_service.set_live(database, [six[-1]], False)
    sap = instrument_service.create(database, SAP).id
    us = [listed(database, "AAPL"), listed(database, "MSFT"), listed(database, "IBM", live=False)]
    monkeypatch.delenv("EODHD_API_KEY")
    monkeypatch.delenv("ALPACA_API_SECRET_KEY")

    # SIX needs no key.
    assert len(price_service.enqueue_due(database)) == 1
    [job] = jobs(database, KIND)
    assert (job.payload, job.max_attempts) == ({"source": "six", "instrument_ids": six[:20]}, 1)

    monkeypatch.setenv("EODHD_API_KEY", "eodhd-key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "alpaca-secret")
    assert len(price_service.enqueue_due(database)) == 2
    # Waiting already: no second job for the same batch.
    assert price_service.enqueue_due(database) == []
    assert [job.payload for job in jobs(database, KIND)][1:] == [
        {"source": "alpaca", "instrument_ids": us[:2]}, {"source": "eodhd", "instrument_ids": [sap]},
    ]


def test_a_fetch_stores_each_price_as_sent_once_and_names_a_wertpapier_without_one(database, market):
    nestle = instrument_service.create(database, NESTLE).id
    unknown = instrument_service.create(database, NESTLE | {"symbol": "XYZ", "valor_id": "CH0012032048CHF4", "name": "Unbekannt"}).id
    apple = listed(database, "AAPL")
    at = datetime(2026, 10, 30, 16, 36, 44, tzinfo=UTC)
    market.prices = {"CH0038863350CHF4": (at.astimezone(instrument_service.six.ZURICH), 79.5), "AAPL": (at, 270.1)}

    price_service.enqueue_due(database)
    run_fetches(database)
    asked_long_ago(database)
    price_service.enqueue_due(database)
    run_fetches(database)

    assert {job.status for job in jobs(database, KIND)} == {JobStatus.SUCCEEDED}
    # Asked twice, the same time: kept once - unadjusted, as sent, with its source and delay.
    stored = {point.instrument_id: point for point in prices(database)}
    assert len(prices(database)) == 2
    assert (str(stored[nestle].close), stored[nestle].at, stored[nestle].source, stored[nestle].delayed) == ("79.500000", at, "six", True)
    assert (str(stored[apple].close), str(stored[apple].previous_close), stored[apple].source, stored[apple].delayed) == (
        "270.100000", "100.000000", "alpaca", False,
    )
    # SIX's robots.txt asked; Alpaca's keys in headers, never in a URL, and Alpaca's API asked without robots.txt.
    hosts = [(urlsplit(url).netloc, urlsplit(url).path) for url, _headers in market.requests]
    assert ("www.six-group.com", "/robots.txt") in hosts and ("data.alpaca.markets", "/robots.txt") not in hosts
    alpaca_headers = [headers for url, headers in market.requests if "alpaca" in url]
    assert alpaca_headers[0]["apca-api-secret-key"] == "alpaca-secret"
    assert not any("alpaca-secret" in url for url, _headers in market.requests)
    assert instrument_row(database, unknown).last_error.startswith("Kein Kurs von SIX")
    view = instrument_service.detail(database, nestle)
    assert (view.price_count, view.instrument.price.price_label, view.instrument.failing) == (1, "79,50 CHF", False)
    assert view.instrument.price.source_label == "SIX, verzögert, 15 Minuten"


def test_a_price_is_asked_for_again_only_after_the_interval(database, market):
    nestle = instrument_service.create(database, NESTLE).id
    market.prices = {"CH0038863350CHF4": (datetime(2026, 10, 30, 17, 0, tzinfo=instrument_service.six.ZURICH), 79.5)}
    platform_settings_service.save(database, {"price_interval_minutes": "60"})

    price_service.enqueue_due(database)
    run_fetches(database)
    fetched = instrument_row(database, nestle).last_fetched_at

    assert price_service.enqueue_due(database, fetched + timedelta(minutes=30)) == []
    assert len(price_service.enqueue_due(database, fetched + timedelta(minutes=60))) == 1


def test_a_refused_key_is_recorded_at_each_wertpapier_of_the_batch_and_fails_the_job(database, market):
    sap = instrument_service.create(database, SAP).id
    market.status = 401

    price_service.enqueue_due(database)
    run_fetches(database)

    [job] = jobs(database, KIND)
    assert job.status == JobStatus.FAILED
    assert instrument_row(database, sap).last_error == "HTTP 401"


def test_a_wertpapier_is_linked_to_its_entity_by_isin_or_by_choice_and_never_entered_twice(database):
    with database.begin() as connection:
        company = entity_repository.create_entity(connection, {"kind": "company", "name": "Nestlé"})
        entity_repository.insert_alias(connection, {"entity_id": company, "scheme": AliasScheme.ISIN, "value": "CH0038863350",
                                                     "key": "CH0038863350", "match": AliasMatch.IDENTIFIER})
        other = entity_repository.create_entity(connection, {"kind": "company", "name": "Roche"})

    # The ISIN from the ValorId.
    by_isin = instrument_service.create(database, NESTLE)
    chosen = instrument_service.create(database, SAP | {"symbol": "ROG", "name": "Roche", "entity": f"Roche (Nr. {other})"})

    assert (by_isin.entity_id, by_isin.entity_name, chosen.entity_id) == (company, "Nestlé", other)
    assert [view.id for view in instrument_service.of_entities(database, [company, other])] == [by_isin.id, chosen.id]
    with pytest.raises(InvalidInstrument, match="CH0038863350CHF4 ist bei SIX bereits erfasst"):
        instrument_service.create(database, NESTLE | {"symbol": "NESN2"})
    with pytest.raises(InvalidInstrument, match="nicht gefunden"):
        instrument_service.update(database, chosen.id, SAP | {"symbol": "ROG", "entity": "Novartis"})


def test_of_a_wertpapier_from_alpacas_list_only_the_entity_changes(database):
    with database.begin() as connection:
        company = entity_repository.create_entity(connection, {"kind": "company", "name": "Apple"})
    apple = listed(database, "AAPL")
    with database.begin() as connection:
        connection.execute(update(Instrument).where(Instrument.id == apple).values(entity_uncertain=True))

    view = instrument_service.update(database, apple, {"symbol": "X", "name": "Changed", "entity": f"Apple (Nr. {company})"})

    assert (view.symbol, view.name, view.entity_id, view.entity_uncertain) == ("AAPL", "AAPL Inc.", company, False)


def test_a_wertpapier_with_prices_or_from_alpacas_list_is_kept_and_one_without_deleted(database, market):
    nestle = instrument_service.create(database, NESTLE).id
    empty = instrument_service.create(database, SAP).id
    apple = listed(database, "AAPL", live=False)
    market.prices = {"CH0038863350CHF4": (datetime(2026, 10, 30, 17, 0, tzinfo=instrument_service.six.ZURICH), 79.5)}
    price_service.enqueue_due(database)
    run_fetches(database)

    assert instrument_service.delete(database, [nestle, empty, apple]).blocked == [
        "Nestlé (hat Kurspunkte; Live-Kurs ausschalten statt löschen)",
        "AAPL Inc. (aus der Liste von Alpaca; Live-Kurs ausschalten statt löschen)",
    ]
    assert instrument_service.delete(database, [empty]).processed == 1
    assert instrument_service.set_live(database, [nestle], False).processed == 1
    asked_long_ago(database)
    assert price_service.enqueue_due(database) == [] and len(prices(database)) == 1


def test_the_list_filters_by_source_live_and_symbol_or_name(database):
    instrument_service.create(database, NESTLE)
    listed(database, "AAPL", live=False)
    listed(database, "MSFT")

    def names(**filters):
        return [view.name for view in instrument_service.overview(database, **filters).instruments]

    assert names() == ["AAPL Inc.", "MSFT Inc.", "Nestlé"]
    assert names(source="alpaca") == ["AAPL Inc.", "MSFT Inc."]
    assert names(live_only=True) == ["MSFT Inc.", "Nestlé"]
    assert names(search="aapl") == ["AAPL Inc."] and names(search="nest") == ["Nestlé"]
    # The symbol searched for first, before names that contain it.
    instrument_service.create(database, NESTLE | {"symbol": "AAAA", "valor_id": "CH0012032048CHF4", "name": "Aaa MSFT tracker"})
    assert names(search="msft") == ["MSFT Inc.", "Aaa MSFT tracker"]
    assert instrument_service.overview(database, source="alpaca").total == 2


def test_the_starter_list_comes_once_and_takes_alpacas_etfs_once_listed(database):
    with database.begin() as connection:
        smi_etf = entity_repository.create_entity(connection, {"kind": "company", "name": "iShares SMI ETF"})
        entity_repository.insert_alias(connection, {"entity_id": smi_etf, "scheme": AliasScheme.ISIN, "value": "CH0008899764",
                                                    "key": "CH0008899764", "match": AliasMatch.IDENTIFIER})
    catalogue = seed_service.instrument_catalogue()
    swiss = [item for item in catalogue if item["source"] == "six"]
    spy = listed(database, "SPY", live=False)

    first = seed_service.import_instruments(database)
    total = len(catalogue)
    # Only SPY is in Alpaca's list yet; the others wait for it.
    assert (first.imported, len(first.waiting)) == (len(swiss) + 1, total - len(swiss) - 1)
    assert instrument_row(database, spy).live
    views = {view.code: view for view in instrument_service.overview(database, source="six").instruments}
    assert views["CSSMI"].entity_id == smi_etf and views["SMI"].currency is None and views["SMI"].kind == "index"

    for symbol in [item["symbol"] for item in catalogue if item["source"] == "alpaca" and item["symbol"] != "SPY"]:
        listed(database, symbol, live=False)
    again = seed_service.import_instruments(database)
    assert (again.imported, again.skipped, again.waiting) == (total - len(swiss) - 1, len(swiss) + 1, [])
    assert seed_service.instruments_status(database).imported == total
