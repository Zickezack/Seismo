"""Alpaca's market data without the network: the requests, and what is made of the answers (app/prices/alpaca.py)."""
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.prices import alpaca
from app.sources.errors import SourceRejected
from app.sources.http import HttpResponse


class FakeHttp:
    """Answers with `body`; records what it was asked."""

    def __init__(self, body):
        self.body, self.requests = body, []

    def get(self, url, accept, permitted=False, headers=None):
        self.requests.append((url, accept, permitted, headers))
        return HttpResponse(url=url, content=json.dumps(self.body).encode() if not isinstance(self.body, bytes) else self.body)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-key")


def query(http):
    return parse_qs(urlsplit(http.requests[0][0]).query)


def test_the_keys_go_in_headers_never_in_the_url_and_the_request_is_permitted():
    http = FakeHttp([])

    alpaca.assets(http, "NASDAQ")

    [(url, accept, permitted, headers)] = http.requests
    assert urlsplit(url).netloc == "paper-api.alpaca.markets" and "secret" not in url and "key-id" not in url
    assert (accept, permitted) == ("application/json", True)
    assert headers == {"APCA-API-KEY-ID": "key-id", "APCA-API-SECRET-KEY": "secret-key"}
    assert query(http) == {"asset_class": ["us_equity"], "exchange": ["NASDAQ"]}


def test_without_the_keys_nothing_is_asked_and_both_variables_are_named(monkeypatch):
    monkeypatch.delenv("ALPACA_API_SECRET_KEY")
    http = FakeHttp([])

    with pytest.raises(SourceRejected, match="ALPACA_API_KEY_ID und ALPACA_API_SECRET_KEY"):
        alpaca.assets(http, "NYSE")

    assert http.requests == [] and not alpaca.configured()


def test_the_list_holds_traded_and_no_longer_traded_wertpapiere_with_their_cusip():
    http = FakeHttp([
        {"id": "a1", "symbol": "aapl", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "status": "active", "cusip": "037833100"},
        {"id": "a2", "symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "exchange": "ARCA", "status": "active"},
        {"id": "a3", "symbol": "TWTR", "name": "Twitter, Inc.", "exchange": "NYSE", "status": "inactive", "cusip": None},
        {"symbol": "NOID"},
    ])

    found = alpaca.assets(http, "NASDAQ")

    assert [(each.ref, each.symbol, each.active, each.cusip, each.etf) for each in found] == [
        ("a1", "AAPL", True, "037833100", False), ("a2", "SPY", True, None, True), ("a3", "TWTR", False, None, False),
    ]


def test_the_calendar_gives_each_trading_day_its_opening_and_close_in_utc():
    http = FakeHttp([{"date": "2026-10-30", "open": "09:30", "close": "16:00"}, {"date": "2026-11-27", "open": "09:30", "close": "13:00"}])

    days = alpaca.calendar(http, date(2026, 10, 30), date(2026, 11, 30))

    assert [(each.day, each.opens_at, each.closes_at) for each in days] == [
        (date(2026, 10, 30), datetime(2026, 10, 30, 13, 30, tzinfo=UTC), datetime(2026, 10, 30, 20, 0, tzinfo=UTC)),
        # After the change to winter time; the day after Thanksgiving closes early.
        (date(2026, 11, 27), datetime(2026, 11, 27, 14, 30, tzinfo=UTC), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),
    ]


def test_daily_prices_are_asked_for_unadjusted_from_all_exchanges_page_by_page():
    http = FakeHttp({"bars": {"AAPL": [{"t": "2024-01-02T05:00:00Z", "o": 187.15, "h": 188.44, "l": 183.885, "c": 185.64, "v": 82488674}],
                              "MSFT": None},
                     "next_page_token": "TOKEN2"})

    bars, token = alpaca.daily_bars(http, ["AAPL", "MSFT"], date(2016, 1, 1), date(2026, 10, 30), "TOKEN1")

    assert query(http) == {
        "symbols": ["AAPL,MSFT"], "timeframe": ["1Day"], "start": ["2016-01-01"], "end": ["2026-10-30"], "limit": ["10000"],
        "adjustment": ["raw"], "feed": ["sip"], "page_token": ["TOKEN1"],
    }
    [bar] = bars
    assert (bar.symbol, bar.at, bar.open, bar.close, bar.volume, token) == (
        "AAPL", datetime(2024, 1, 2, 5, tzinfo=UTC), Decimal("187.15"), Decimal("185.64"), 82488674, "TOKEN2",
    )


def test_a_wertpapier_no_longer_traded_is_asked_for_by_its_symbol_as_it_was():
    http = FakeHttp({"bars": {}, "next_page_token": None})

    bars, token = alpaca.daily_bars(http, ["TWTR"], date(2016, 1, 1), date(2022, 10, 27), follow_renames=False)

    assert query(http)["asof"] == ["-"] and (bars, token) == ([], None)


def test_corporate_actions_name_their_wertpapiere_by_symbol_and_cusip():
    http = FakeHttp({"corporate_actions": {
        "forward_splits": [{"id": "s1", "symbol": "NVDA", "cusip": "67066G104", "old_rate": 1, "new_rate": 10,
                            "ex_date": "2024-06-10", "process_date": "2024-06-10"}],
        "name_changes": [{"id": "n1", "old_symbol": "FB", "old_cusip": "30303M102", "new_symbol": "META", "new_cusip": "30303M102",
                          "process_date": "2022-06-09"}],
        "cash_mergers": [{"id": "m1", "acquirer_symbol": None, "acquiree_symbol": "TWTR", "acquiree_cusip": "90184L102",
                          "rate": 54.2, "effective_date": "2022-10-28", "process_date": "2022-10-28"}],
    }, "next_page_token": None})

    actions, token = alpaca.corporate_actions(http, date(2022, 1, 1), date(2022, 3, 31))

    assert query(http)["types"][0].startswith("forward_split,reverse_split") and query(http)["limit"] == ["1000"]
    by_kind = {action.kind: action for action in actions}
    split, rename, merger = by_kind["forward_split"], by_kind["name_change"], by_kind["cash_merger"]
    assert (split.kind, split.parties, split.old_rate, split.new_rate, split.ex_date) == (
        "forward_split", [("NVDA", "67066G104")], Decimal(1), Decimal(10), date(2024, 6, 10),
    )
    assert (rename.parties, rename.old_symbol, rename.new_symbol) == ([("META", "30303M102")], "FB", "META")
    # The acquired one first, even with an acquirer that has none.
    assert (merger.parties, merger.rate, merger.effective_date) == ([("TWTR", "90184L102"), (None, None)], Decimal("54.2"), date(2022, 10, 28))
    assert token is None


def test_the_latest_price_is_the_last_trade_with_nanoseconds_read_to_microseconds():
    http = FakeHttp({
        "AAPL": {"latestTrade": {"t": "2026-10-30T19:59:59.123456789Z", "p": 270.1},
                 "dailyBar": {"o": 268, "h": 271, "l": 267.5, "c": 270.1, "v": 4100000}, "prevDailyBar": {"c": 268.5}},
        "XYZ": {},
    })

    quotes, missing = alpaca.latest(http, ["AAPL", "XYZ"])

    assert query(http) == {"symbols": ["AAPL,XYZ"], "feed": ["iex"]}
    [quote] = quotes
    assert (quote.ref, quote.at, quote.close, quote.open, quote.volume, quote.previous_close) == (
        "AAPL", datetime(2026, 10, 30, 19, 59, 59, 123456, tzinfo=UTC), Decimal("270.1"), Decimal(268), 4100000, Decimal("268.5"),
    )
    assert [each.ref for each in missing] == ["XYZ"] and missing[0].reason.startswith("Kein Kurs von Alpaca")


@pytest.mark.parametrize("body", [b"<html>", {"bars": ["no"]}])
def test_an_answer_that_is_no_list_of_daily_prices_is_rejected(body):
    with pytest.raises(SourceRejected, match="Antwort von Alpaca"):
        alpaca.daily_bars(FakeHttp(body), ["AAPL"], date(2026, 1, 1), date(2026, 1, 31))


def test_a_tracked_wertpapier_s_minutes_are_asked_for_from_all_exchanges_by_the_minute():
    http = FakeHttp({"bars": {"AAPL": [{"t": "2026-11-04T14:31:00Z", "o": 222.1, "h": 222.5, "l": 222.0, "c": 222.4, "v": 18211}]},
                     "next_page_token": None})

    bars, token = alpaca.minute_bars(
        http, ["AAPL"], datetime(2026, 11, 4, 14, 30, 15, tzinfo=UTC), datetime(2026, 11, 4, 15, 0, tzinfo=UTC), "T2",
    )

    assert query(http) == {
        "symbols": ["AAPL"], "timeframe": ["1Min"], "start": ["2026-11-04T14:30:15Z"], "end": ["2026-11-04T15:00:00Z"],
        "limit": ["10000"], "adjustment": ["raw"], "feed": ["sip"], "page_token": ["T2"],
    }
    [bar] = bars
    assert (bar.at, bar.open, bar.close, bar.volume, token) == (datetime(2026, 11, 4, 14, 31, tzinfo=UTC), Decimal("222.1"), Decimal("222.4"), 18211, None)


def test_an_answer_without_minutes_is_refused():
    with pytest.raises(SourceRejected, match="keine Minutenkurse"):
        alpaca.minute_bars(FakeHttp({"bars": ["AAPL"]}), ["AAPL"], datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 5, tzinfo=UTC))
