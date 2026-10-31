"""EODHD's live prices without the network: the request, and what is made of the answer (app/prices/eodhd.py)."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.prices import eodhd
from app.sources.errors import SourceRejected
from app.sources.http import HttpResponse

APPLE = {"code": "AAPL.US", "timestamp": 1793392140, "gmtoffset": 0, "open": 346.715, "high": 349.19, "low": 334.35,
         "close": 344.63, "volume": 67488898, "previousClose": 348.91, "change": -4.28, "change_p": -1.2267}


class FakeHttp:
    """Answers with `body`; records what it was asked."""

    def __init__(self, body):
        self.body, self.requests = body, []

    def get(self, url, accept, permitted=False):
        self.requests.append((url, accept, permitted))
        return HttpResponse(url=url, content=json.dumps(self.body).encode() if not isinstance(self.body, bytes) else self.body)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("EODHD_API_KEY", "secret-key")


def test_one_request_asks_for_every_wertpapier_of_the_batch_with_permission():
    http = FakeHttp([APPLE, APPLE | {"code": "NESN.SW", "close": 79.5}])

    quotes, missing = eodhd.latest(http, [("AAPL", "US"), ("NESN", "SW")])

    [(url, accept, permitted)] = http.requests
    parts = urlsplit(url)
    assert (parts.netloc, parts.path, accept, permitted) == ("eodhd.com", "/api/real-time/AAPL.US", "application/json", True)
    assert parse_qs(parts.query) == {"api_token": ["secret-key"], "fmt": ["json"], "s": ["NESN.SW"]}
    assert [(quote.symbol, quote.exchange, quote.close) for quote in quotes] == [("AAPL", "US", Decimal("344.63")), ("NESN", "SW", Decimal("79.5"))]
    assert missing == []


def test_a_price_is_kept_as_sent_with_its_own_time_in_utc():
    [quote], _ = eodhd.latest(FakeHttp(APPLE), [("AAPL", "US")])

    assert quote.at == datetime(2026, 10, 30, 20, 29, tzinfo=UTC)
    assert (quote.open, quote.high, quote.low, quote.volume, quote.previous_close) == (
        Decimal("346.715"), Decimal("349.19"), Decimal("334.35"), 67488898, Decimal("348.91"),
    )


def test_a_wertpapier_without_a_price_is_named_with_why():
    http = FakeHttp([APPLE | {"code": "XYZ.US", "timestamp": "NA", "close": "NA"}])

    quotes, missing = eodhd.latest(http, [("XYZ", "US"), ("AAPL", "US")])

    assert quotes == []
    assert [(each.symbol, each.exchange) for each in missing] == [("XYZ", "US"), ("AAPL", "US")]
    assert missing[0].reason.startswith("Kein Kurs von EODHD")


@pytest.mark.parametrize("body", [b"<html>Fehler</html>", ["not a price"]])
def test_an_answer_that_is_no_list_of_prices_is_rejected(body):
    with pytest.raises(SourceRejected, match="Antwort von EODHD"):
        eodhd.latest(FakeHttp(body), [("AAPL", "US")])


def test_without_a_key_nothing_is_asked_and_the_variable_is_named(monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY")
    http = FakeHttp([APPLE])

    with pytest.raises(SourceRejected) as error:
        eodhd.latest(http, [("AAPL", "US")])

    assert "EODHD_API_KEY" in str(error.value) and http.requests == [] and not eodhd.configured()
