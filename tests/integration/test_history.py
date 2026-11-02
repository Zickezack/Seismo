"""The historical market data in PostgreSQL from a simulated Alpaca (history_service): the list exchange by exchange,
the calendar, the corporate actions and the daily prices as chains of jobs; the links to entities; and the daily
prices as known at a point in time."""
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from sqlalchemy import delete, func, select, text

from app.db.models import AliasMatch, AliasScheme, Instrument, JobStatus, PricePoint, TradingDay
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.prices import alpaca
from app.repositories import entity_repository, market_data_repository
from app.services import history_service, instrument_service, job_service, platform_settings_service
from app.sources.http import HttpClient
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_ingestion import jobs

KINDS = history_service.CHAIN_KINDS
TODAY = datetime.now(UTC).date()
# Two years: the period always reaches far enough back for a split and a merger in its first months.
YEARS = 2
START = history_service.period_start(TODAY, YEARS)


def weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


SPLIT_DAY = weekday(START + timedelta(days=40))
MERGER_DAY = weekday(START + timedelta(days=60))
RECENT_DAY = weekday(TODAY - timedelta(days=30))
ASSETS = {
    "NASDAQ": [
        {"id": "id-aapl", "symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "status": "active", "cusip": "037833100"},
        {"id": "id-msft", "symbol": "MSFT", "name": "Microsoft Corporation Common Stock", "exchange": "NASDAQ", "status": "active"},
    ],
    "NYSE": [{"id": "id-twtr", "symbol": "TWTR", "name": "Twitter, Inc. Common Stock", "exchange": "NYSE", "status": "inactive",
              "cusip": "90184L102"}],
    "ARCA": [{"id": "id-spy", "symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "exchange": "ARCA", "status": "active"}],
}
ACTIONS = {
    "forward_splits": [{"id": "split-aapl", "symbol": "AAPL", "cusip": "037833100", "old_rate": 1, "new_rate": 4,
                        "ex_date": SPLIT_DAY.isoformat(), "process_date": (SPLIT_DAY - timedelta(days=3)).isoformat()}],
    "cash_mergers": [{"id": "merger-twtr", "acquiree_symbol": "TWTR", "acquiree_cusip": "90184L102", "rate": 54.2,
                      "effective_date": MERGER_DAY.isoformat(), "process_date": MERGER_DAY.isoformat()}],
    "cash_dividends": [
        {"id": "dividend-unknown", "symbol": "ZZZZ", "rate": 0.1, "ex_date": SPLIT_DAY.isoformat(), "process_date": SPLIT_DAY.isoformat()},
        # Recent: Microsoft, whose listing has no CUSIP, takes this one's.
        {"id": "dividend-msft", "symbol": "MSFT", "cusip": "594918104", "rate": 0.91, "currency": "USD",
         "ex_date": RECENT_DAY.isoformat(), "process_date": RECENT_DAY.isoformat()},
        # Long ago: a CUSIP that may be of a company that had the symbol before - not taken.
        {"id": "dividend-spy-old", "symbol": "SPY", "cusip": "78462F103", "rate": 1.5, "currency": "USD",
         "ex_date": MERGER_DAY.isoformat(), "process_date": MERGER_DAY.isoformat()},
    ],
}


def trading_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=n) for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5]


class Alpaca:
    """Alpaca's list, calendar, corporate actions and daily prices: every weekday a trading day closing at 16:00 in New
    York, every symbol a price of 100 each day - 400 for AAPL before its split -, the first symbol of a request on a
    page of its own."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        url = str(request.url)
        self.requests.append(url)
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        path = request.url.path
        if path == "/v2/assets":
            return httpx2.Response(200, json=ASSETS.get(query["exchange"], []))
        if path == "/v2/calendar":
            days = trading_days(date.fromisoformat(query["start"]), date.fromisoformat(query["end"]))
            return httpx2.Response(200, json=[{"date": day.isoformat(), "open": "09:30", "close": "16:00"} for day in days])
        if path == "/v1/corporate-actions":
            start, end = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
            found = {
                array: [item for item in items if start <= date.fromisoformat(item["process_date"]) <= end]
                for array, items in ACTIONS.items()
            }
            return httpx2.Response(200, json={"corporate_actions": found, "next_page_token": None})
        if path == "/v2/stocks/bars":
            symbols = query["symbols"].split(",")
            page = symbols[1:] if query.get("page_token") else symbols[:1]
            days = trading_days(date.fromisoformat(query["start"]), date.fromisoformat(query["end"]))
            bars = {
                symbol: [
                    {"t": datetime.combine(day, time(0), alpaca.NEW_YORK).astimezone(UTC).isoformat().replace("+00:00", "Z"),
                     "o": price, "h": price, "l": price, "c": price, "v": 1000}
                    for day in days for price in [400 if symbol == "AAPL" and day < SPLIT_DAY else 100]
                ]
                for symbol in page
            }
            more = len(symbols) > 1 and not query.get("page_token")
            return httpx2.Response(200, json={"bars": bars, "next_page_token": "page-2" if more else None})
        return httpx2.Response(404)


@pytest.fixture
def market(monkeypatch):
    fake = Alpaca()
    monkeypatch.setenv("ALPACA_API_KEY_ID", "alpaca-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "alpaca-secret")
    monkeypatch.setattr(history_service, "HttpClient", lambda contact: HttpClient(contact, httpx2.MockTransport(fake)))
    return fake


@pytest.fixture
def period(database):
    platform_settings_service.save(database, {"price_history_years": str(YEARS)})


def run_chains(database, rounds=200):
    """Run the chains' jobs, the hosts free before each one - as if each waited for its slot - and start the next
    chain whenever one ends, until nothing is due."""
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    for _ in range(rounds):
        with database.begin() as connection:
            connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = now() - interval '1 minute'"))
            connection.execute(text("UPDATE job SET run_after = now() WHERE status = 'queued'"))
        if run_next_job(database, {kind: handlers[kind] for kind in KINDS}, "test", 10, on_renewed=lambda: None):
            continue
        if not history_service.enqueue_due(database):
            return
    raise AssertionError("The chains did not end")


def instrument(database, symbol):
    with database.connect() as connection:
        return connection.execute(select(Instrument.__table__).where(Instrument.symbol == symbol)).one()


def daily_prices(database, symbol):
    with database.connect() as connection:
        statement = (
            select(PricePoint.__table__).join(Instrument, Instrument.id == PricePoint.instrument_id)
            .where(Instrument.symbol == symbol, PricePoint.resolution == "day").order_by(PricePoint.at)
        )
        return connection.execute(statement).all()


def test_nothing_starts_without_the_keys(database, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)

    assert history_service.enqueue_due(database) == []


def test_the_list_comes_exchange_by_exchange_then_the_calendar_and_links_the_entities(database, market, period):
    with database.begin() as connection:
        apple = entity_repository.create_entity(connection, {"kind": "company", "name": "Apple"})
        entity_repository.insert_alias(connection, {"entity_id": apple, "scheme": AliasScheme.ISIN, "value": "US0378331005",
                                                    "key": "US0378331005", "match": AliasMatch.IDENTIFIER})
        microsoft = entity_repository.create_entity(connection, {"kind": "company", "name": "Microsoft Corp."})
        entity_repository.insert_alias(connection, {"entity_id": microsoft, "scheme": AliasScheme.TICKER, "value": "NASDAQ:MSFT",
                                                    "key": "NASDAQ:MSFT", "match": AliasMatch.IDENTIFIER})
        twitter = entity_repository.create_entity(connection, {"kind": "company", "name": "Twitter"})
        entity_repository.insert_alias(connection, {"entity_id": twitter, "scheme": AliasScheme.NAME, "value": "Twitter",
                                                    "key": "twitter", "match": AliasMatch.FOUNDING})

    [first] = history_service.enqueue_due(database)
    # Under way: not started twice.
    assert history_service.enqueue_due(database) == []
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    kind = job_service.SYNC_MARKET_JOB_KIND
    for _ in range(len(alpaca.EXCHANGES) + 1):
        with database.begin() as connection:
            connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = now() - interval '1 minute'"))
        assert run_next_job(database, {kind: handlers[kind]}, "test", 10, on_renewed=lambda: None)

    steps = jobs(database, kind)
    assert [job.payload["step"] for job in steps] == list(range(len(alpaca.EXCHANGES) + 1))
    assert {job.status for job in steps} == {JobStatus.SUCCEEDED} and steps[0].id == first and steps[0].priority == 1
    aapl, twtr, spy = instrument(database, "AAPL"), instrument(database, "TWTR"), instrument(database, "SPY")
    assert (aapl.source, aapl.source_ref, aapl.isin, aapl.kind, aapl.live, aapl.history) == (
        "alpaca", "id-aapl", "US0378331005", "stock", False, True,
    )
    assert (twtr.status, spy.kind) == ("inactive", "etf")
    # By ISIN and by ticker certain, by name uncertain.
    assert (aapl.entity_id, aapl.entity_uncertain) == (apple, False)
    assert (instrument(database, "MSFT").entity_id, instrument(database, "MSFT").entity_uncertain) == (microsoft, False)
    assert (twtr.entity_id, twtr.entity_uncertain) == (twitter, True)
    with database.connect() as connection:
        days = connection.execute(select(func.min(TradingDay.day), func.max(TradingDay.day))).one()
        assert days == (weekday(START), trading_days(date(TODAY.year + 1, 12, 24), date(TODAY.year + 1, 12, 31))[-1])
        assert market_data_repository.sync(connection, history_service.ASSETS_SYNC).through == TODAY

    # A symbol that changes keeps its Wertpapier: Alpaca's id stays.
    ASSETS["NASDAQ"][0]["symbol"] = "APPL"
    try:
        with database.begin() as connection:
            connection.execute(text("UPDATE market_data_sync SET synced_at = now() - interval '1 day' WHERE name = 'assets'"))
        run_chains(database)
    finally:
        ASSETS["NASDAQ"][0]["symbol"] = "AAPL"
    assert instrument(database, "APPL").id == aapl.id


def test_corporate_actions_then_daily_prices_reach_the_last_closed_day(database, market, period):
    run_chains(database)

    with database.connect() as connection:
        closed = market_data_repository.last_closed_day(connection, datetime.now(UTC) - history_service.SETTLE)
        assert market_data_repository.sync(connection, history_service.ACTIONS_SYNC).through == TODAY - timedelta(days=1)
    aapl, twtr = instrument(database, "AAPL"), instrument(database, "TWTR")
    expected = trading_days(START, closed)
    assert [bar.at.astimezone(alpaca.NEW_YORK).date() for bar in daily_prices(database, "AAPL")] == expected
    assert (aapl.history_through, daily_prices(database, "AAPL")[0].source, daily_prices(database, "AAPL")[0].delayed) == (closed, "alpaca", False)
    # Unadjusted, as received.
    assert {str(bar.close) for bar in daily_prices(database, "AAPL")} == {"400.000000", "100.000000"}
    # The merger ends Twitter: none of its symbol's prices after, and asked for by its symbol as it was.
    assert twtr.delisted_on == MERGER_DAY
    assert daily_prices(database, "TWTR")[-1].at.astimezone(alpaca.NEW_YORK).date() == MERGER_DAY
    assert any("symbols=TWTR" in url and "asof=-" in url for url in market.requests)
    assert not any("symbols=AAPL" in url and "asof=" in url for url in market.requests)
    # Each Kapitalmassnahme at its Wertpapier, one of an unknown Wertpapier left out.
    detail = instrument_service.detail(database, aapl.id)
    assert [(action.kind, action.summary) for action in detail.actions] == [("forward_split", "1 : 4")]
    assert [action.kind for action in instrument_service.detail(database, twtr.id).actions] == ["cash_merger"]
    msft, spy = instrument(database, "MSFT"), instrument(database, "SPY")
    assert (msft.cusip, msft.isin, spy.cusip, spy.isin) == ("594918104", "US5949181045", None, None)
    assert [action.kind for action in instrument_service.detail(database, spy.id).actions] == ["cash_dividend"]
    assert (detail.coverage.bars, detail.coverage.missing, detail.coverage.gaps) == (len(expected), 0, [])

    status = instrument_service.overview(database).market
    assert (status.total, status.done, status.running, status.closed_day) == (4, 4, False, closed)
    # The fetch is done: nothing more is due.
    assert history_service.enqueue_due(database) == []


def test_a_point_in_time_sees_only_the_closed_days_and_the_splits_known_then(database, market, period):
    run_chains(database)
    aapl = instrument(database, "AAPL").id
    before_split = SPLIT_DAY - timedelta(days=1)
    with database.connect() as connection:
        close = connection.execute(select(TradingDay.closes_at).where(TradingDay.day == weekday(before_split - timedelta(days=3)))).scalar_one()

        # A minute before a day's close its prices are not known yet.
        bars = history_service.known_on(connection, aapl, close - timedelta(minutes=1), 5)
        assert bars[0].day < close.astimezone(alpaca.NEW_YORK).date()
        # Before the split took effect - though announced -, nothing adjusted.
        bars = history_service.known_on(connection, aapl, close, 5)
        assert bars[0].day == close.astimezone(alpaca.NEW_YORK).date() and {bar.factor for bar in bars} == {1}
        # From its day on, the prices before it adjusted by the split known then.
        after = datetime.combine(SPLIT_DAY, time(23), alpaca.NEW_YORK)
        bars = history_service.known_on(connection, aapl, after, 5)
    assert (bars[0].day, bars[0].factor, bars[0].adjusted_close) == (SPLIT_DAY, 1, Decimal(100))
    assert (bars[1].factor, bars[1].close, bars[1].adjusted_close) == (Decimal("0.25"), Decimal(400), Decimal(100))

    detail = instrument_service.detail(database, aapl, instrument_service.parse_as_of(SPLIT_DAY.isoformat()))
    assert detail.bars[0].day == SPLIT_DAY


def test_a_missing_day_shows_as_a_gap(database, market, period):
    run_chains(database)
    aapl = instrument(database, "AAPL").id
    gap = weekday(START + timedelta(days=20))
    with database.begin() as connection:
        connection.execute(delete(PricePoint).where(
            PricePoint.instrument_id == aapl, PricePoint.at == datetime.combine(gap, time(0), alpaca.NEW_YORK)
        ))

    coverage = instrument_service.detail(database, aapl).coverage

    assert (coverage.missing, coverage.gaps) == (1, [gap])


def test_a_longer_period_fetches_everything_again_from_its_start(database, market, period):
    run_chains(database)
    platform_settings_service.save(database, {"price_history_years": str(YEARS + 1)})

    history_service.enqueue_due(database)

    assert instrument(database, "AAPL").history_through is None
    with database.connect() as connection:
        assert market_data_repository.sync(connection, history_service.ACTIONS_SYNC).through is None
        assert market_data_repository.sync(connection, history_service.PERIOD_SYNC).through == history_service.period_start(TODAY, YEARS + 1)
    assert [job.payload["start"] for job in jobs(database, job_service.FETCH_ACTIONS_JOB_KIND)][-1] == (
        history_service.period_start(TODAY, YEARS + 1).isoformat()
    )


def test_a_batch_running_while_the_period_grows_stays_to_be_fetched_from_the_new_start(database, market, period):
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    kind = job_service.FETCH_BARS_JOB_KIND

    def run_one(kinds):
        with database.begin() as connection:
            connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = now() - interval '1 minute'"))
            connection.execute(text("UPDATE job SET run_after = now() WHERE status = 'queued'"))
        return run_next_job(database, {each: handlers[each] for each in kinds}, "test", 10, on_renewed=lambda: None)

    # The list and the corporate actions first, until the daily prices start.
    while not jobs(database, kind):
        if not run_one(KINDS[:2]):
            history_service.enqueue_due(database)
    assert run_one([kind])  # the batch's first page; its second is queued
    platform_settings_service.save(database, {"price_history_years": str(YEARS + 1)})
    history_service.enqueue_due(database)
    assert run_one([kind])  # the batch's last page

    assert instrument(database, "AAPL").history_through is None and daily_prices(database, "AAPL")
