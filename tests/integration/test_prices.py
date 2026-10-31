"""Wertpapiere and their prices in PostgreSQL: entered and imported, linked to entities, and fetched once a minute
from a simulated EODHD (price_service, instrument_service)."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from sqlalchemy import select, text

from app.db.models import AliasMatch, AliasScheme, Instrument, JobStatus, PricePoint
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.repositories import entity_repository
from app.services import instrument_service, job_service, price_service, seed_service
from app.services.instrument_service import InvalidInstrument
from app.sources.http import HttpClient
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_ingestion import jobs

KIND = job_service.FETCH_PRICES_JOB_KIND
FORM = {"symbol": "NESN", "exchange": "SW", "name": "Nestlé", "kind": "stock", "currency": "CHF"}


class Eodhd:
    """Answers EODHD's live price requests with `prices` (code: (timestamp, close)), or with `status`."""

    def __init__(self):
        self.prices, self.status, self.requests = {}, 200, []

    def __call__(self, request):
        self.requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, text="User-agent: *\\nDisallow: /api/\\n")
        if self.status != 200:
            return httpx2.Response(self.status)
        first = request.url.path.rsplit("/", 1)[1]
        codes = [first, *filter(None, parse_qs(urlsplit(str(request.url)).query).get("s", [""])[0].split(","))]
        body = [
            {"code": code, "timestamp": self.prices[code][0], "close": self.prices[code][1], "previousClose": 100}
            if code in self.prices else {"code": code, "timestamp": "NA", "close": "NA"}
            for code in codes
        ]
        return httpx2.Response(200, json=body)


@pytest.fixture
def eodhd(monkeypatch):
    fake = Eodhd()
    monkeypatch.setenv("EODHD_API_KEY", "secret-key")
    monkeypatch.setattr(price_service, "HttpClient", lambda contact: HttpClient(contact, httpx2.MockTransport(fake)))
    return fake


def run_fetches(database):
    """Run the queued fetches, EODHD's host free again first: as a minute later."""
    with database.begin() as connection:
        connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = now() - interval '1 minute'"))
        connection.execute(text("UPDATE job SET run_after = now() WHERE status = 'queued'"))
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    while run_next_job(database, {KIND: handlers[KIND]}, "test", 10, on_renewed=lambda: None):
        pass


def prices(database):
    with database.connect() as connection:
        return connection.execute(select(PricePoint.__table__).order_by(PricePoint.id)).all()


def instrument_row(database, instrument_id):
    with database.connect() as connection:
        return connection.execute(select(Instrument.__table__).where(Instrument.id == instrument_id)).one()


def test_the_worker_fetches_every_active_wertpapier_in_batches_only_with_the_key(database, eodhd, monkeypatch):
    ids = [instrument_service.create(database, FORM | {"symbol": f"S{number}", "name": f"Aktie {number}"}).id for number in range(21)]
    instrument_service.set_active(database, [ids[-1]], False)
    monkeypatch.delenv("EODHD_API_KEY")
    assert price_service.enqueue_due(database) == []

    monkeypatch.setenv("EODHD_API_KEY", "secret-key")
    assert len(price_service.enqueue_due(database)) == 1
    # Waiting already: no second job for the same batch.
    assert price_service.enqueue_due(database) == []
    [job] = jobs(database, KIND)
    assert (job.payload["instrument_ids"], job.max_attempts) == (ids[:20], 1)


def test_a_fetch_stores_each_price_as_sent_once_and_names_a_wertpapier_without_one(database, eodhd):
    nestle = instrument_service.create(database, FORM).id
    unknown = instrument_service.create(database, FORM | {"symbol": "XYZ", "name": "Unbekannt"}).id
    eodhd.prices = {"NESN.SW": (1793392140, 79.5)}

    price_service.enqueue_due(database)
    run_fetches(database)
    price_service.enqueue_due(database)
    run_fetches(database)

    assert [job.status for job in jobs(database, KIND)] == [JobStatus.SUCCEEDED, JobStatus.SUCCEEDED]
    # Asked twice, the same time: kept once - unadjusted, as sent, with its source and delay.
    [point] = prices(database)
    assert (point.instrument_id, str(point.close), str(point.previous_close), point.source, point.delayed) == (nestle, "79.500000", "100.000000", "eodhd", True)
    assert point.at.timestamp() == 1793392140 and point.received_at is not None
    # The key never in a URL the policy asked robots.txt for: EODHD's API with the key is permitted.
    assert all("/robots.txt" not in url for url in eodhd.requests)
    assert parse_qs(urlsplit(eodhd.requests[0]).query)["s"] == ["XYZ.SW"]
    assert instrument_row(database, unknown).last_error.startswith("Kein Kurs von EODHD")
    assert instrument_service.detail(database, unknown).instrument.failing
    view = instrument_service.detail(database, nestle)
    assert (view.price_count, view.instrument.price.price_label, view.instrument.failing) == (1, "79,50 CHF", False)

    eodhd.prices = {"NESN.SW": (1793392200, 79.6)}
    price_service.enqueue_due(database)
    run_fetches(database)
    assert [str(point.close) for point in prices(database)] == ["79.500000", "79.600000"]
    assert instrument_service.detail(database, nestle).instrument.price.price_label == "79,60 CHF"


def test_a_refused_key_is_recorded_at_each_wertpapier_of_the_batch_and_fails_the_job(database, eodhd):
    nestle = instrument_service.create(database, FORM).id
    eodhd.status = 401

    price_service.enqueue_due(database)
    run_fetches(database)

    [job] = jobs(database, KIND)
    assert job.status == JobStatus.FAILED
    assert instrument_row(database, nestle).last_error == "HTTP 401"


def test_a_wertpapier_is_linked_to_its_entity_by_isin_or_by_choice_and_never_entered_twice(database):
    with database.begin() as connection:
        company = entity_repository.create_entity(connection, {"kind": "company", "name": "Nestlé"})
        entity_repository.insert_alias(connection, {"entity_id": company, "scheme": AliasScheme.ISIN, "value": "CH0038863350",
                                                     "key": "CH0038863350", "match": AliasMatch.IDENTIFIER})
        other = entity_repository.create_entity(connection, {"kind": "company", "name": "Roche"})

    by_isin = instrument_service.create(database, FORM | {"isin": "ch0038863350"})
    chosen = instrument_service.create(database, FORM | {"symbol": "ROG", "name": "Roche", "entity": f"Roche (Nr. {other})"})

    assert (by_isin.entity_id, by_isin.entity_name, chosen.entity_id) == (company, "Nestlé", other)
    assert [view.id for view in instrument_service.of_entities(database, [company, other])] == [by_isin.id, chosen.id]
    with pytest.raises(InvalidInstrument, match="NESN.SW ist bereits erfasst"):
        instrument_service.create(database, FORM)
    with pytest.raises(InvalidInstrument, match="nicht gefunden"):
        instrument_service.update(database, chosen.id, FORM | {"symbol": "ROG", "entity": "Novartis"})


def test_a_wertpapier_with_prices_is_kept_and_one_without_deleted(database, eodhd):
    nestle = instrument_service.create(database, FORM).id
    empty = instrument_service.create(database, FORM | {"symbol": "ROG", "name": "Roche"}).id
    eodhd.prices = {"NESN.SW": (1793392140, 79.5)}
    price_service.enqueue_due(database)
    run_fetches(database)

    assert instrument_service.delete(database, [nestle, empty]).blocked == ["Nestlé (hat Kurspunkte; deaktivieren statt löschen)"]
    assert instrument_service.delete(database, [empty]).processed == 1
    assert instrument_service.set_active(database, [nestle], False).processed == 1
    assert price_service.enqueue_due(database) == [] and len(prices(database)) == 1


def test_the_starter_list_comes_once_and_links_by_isin(database):
    with database.begin() as connection:
        etf = entity_repository.create_entity(connection, {"kind": "company", "name": "SPDR S&P 500 ETF"})
        entity_repository.insert_alias(connection, {"entity_id": etf, "scheme": AliasScheme.ISIN, "value": "US78462F1030",
                                                    "key": "US78462F1030", "match": AliasMatch.IDENTIFIER})

    first = seed_service.import_instruments(database)
    again = seed_service.import_instruments(database)

    total = len(seed_service.instrument_catalogue())
    assert (first.imported, again.imported, again.skipped) == (total, 0, total)
    listed = {view.code: view for view in instrument_service.overview(database).instruments}
    assert listed["SPY.US"].entity_id == etf and listed["GSPC.INDX"].currency is None and listed["SSMI.INDX"].kind == "index"
    assert seed_service.instruments_status(database).imported == total
