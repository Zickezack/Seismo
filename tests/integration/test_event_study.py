"""The event study in PostgreSQL (userstories.md, Historische Markt- und Ereignisdaten, story 2): an event naming a
company and a sector, its Wertpapier against SPY and the sector's ETF, with a split, a dividend and a gap in its prices;
the overview by period; the benchmarks' prices first in the daily sync."""
import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, insert, update

from app.db.models import CorporateAction, EntityAlias, Instrument, PricePoint, TradingDay
from app.repositories import instrument_repository
from app.services import event_study_service, platform_settings_service
from app.services.event_study_service import Benchmark, Exclusion, Measure, Period, Status
from tests.integration.test_dashboard import STRIKE
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_context import event_of, reported
from tests.integration.test_events import QUAKE
from tests.integration.test_event_filter import named

NEW_YORK = ZoneInfo("America/New_York")
CALENDAR = [date(2024, 10, 1) + timedelta(days=n) for n in range(456) if (date(2024, 10, 1) + timedelta(days=n)).weekday() < 5]
# Reported during the session of Tuesday 10 June: that day is the Ereignistag.
REPORTED_AT = datetime(2025, 6, 10, 14, tzinfo=UTC)
EVENT_DAY = date(2025, 6, 10)
START = CALENDAR.index(EVENT_DAY)
SPLIT_DAY, DIVIDEND_DAY, GAP_DAY = CALENDAR[START - 60], CALENDAR[START - 40], CALENDAR[START + 3]
LAST_FETCHED = date(2025, 6, 30)
NOW = datetime(2025, 7, 1, 22, tzinfo=UTC)
JUMP = 0.05


def instrument(connection, symbol, kind="stock", entity_id=None):
    values = {
        "source": "alpaca", "source_ref": f"id-{symbol.lower()}", "symbol": symbol, "exchange": "NYSE", "name": f"{symbol} Inc.",
        "kind": kind, "currency": "USD", "history": True, "history_through": LAST_FETCHED, "entity_id": entity_id, "live": False,
    }
    return connection.execute(insert(Instrument).values(values).returning(Instrument.id)).scalar_one()


def closes(returns, splits=None, dividends=None):
    """Unadjusted closes from daily returns: the price falls by a dividend going ex and by a split's ratio."""
    splits, dividends = splits or {}, dividends or {}
    prices, price = [], 100.0
    for index, day in enumerate(CALENDAR):
        if index:
            price = (price * (1 + returns[index]) - dividends.get(day, 0)) * splits.get(day, 1)
        prices.append(price)
    return prices


def store(connection, instrument_id, prices):
    rows = [
        {"instrument_id": instrument_id, "resolution": "day", "at": datetime.combine(day, time(), NEW_YORK), "open": price,
         "high": price, "low": price, "close": Decimal(f"{price:.6f}"), "volume": 1000, "source": "alpaca", "delayed": False}
        for day, price in zip(CALENDAR, prices) if day <= LAST_FETCHED
    ]
    connection.execute(insert(PricePoint), rows)


@pytest.fixture
def market(database):
    """SPY, the oil ETF XLE and Exco, a company the report of a strike names with the sector Öl und Gas: Exco moves 1.5
    times the market and 5 % more on the Ereignistag, split one into two and paid a dividend before, and lacks a price
    three days after."""
    candidate = reported(database, "x", STRIKE, published_at=REPORTED_AT)
    exco = named(database, candidate, "company", "Exco")
    oil = named(database, candidate, "sector", "Öl und Gas", position=1)
    rng = random.Random(7)
    market_returns = [rng.gauss(0.0004, 0.01) for _ in CALENDAR]
    oil_returns = [0.8 * value + rng.gauss(0, 0.006) for value in market_returns]
    stock_returns = [0.0002 + 1.5 * value + rng.gauss(0, 0.004) for value in market_returns]
    stock_returns[START] += JUMP
    with database.begin() as connection:
        connection.execute(insert(EntityAlias).values(entity_id=oil, scheme="catalogue", value="oil-gas", key="oil-gas", match="founding"))
        connection.execute(insert(TradingDay), [
            {"day": day, "opens_at": datetime.combine(day, time(9, 30), NEW_YORK), "closes_at": datetime.combine(day, time(16), NEW_YORK)}
            for day in CALENDAR
        ])
        ids = {
            "SPY": instrument(connection, "SPY", "etf"), "XLE": instrument(connection, "XLE", "etf"),
            "XCO": instrument(connection, "XCO", entity_id=exco),
        }
        store(connection, ids["SPY"], closes(market_returns))
        store(connection, ids["XLE"], closes(oil_returns))
        store(connection, ids["XCO"], closes(stock_returns, {SPLIT_DAY: 0.5}, {DIVIDEND_DAY: 0.5}))
        connection.execute(delete(PricePoint).where(
            PricePoint.instrument_id == ids["XCO"], PricePoint.at == datetime.combine(GAP_DAY, time(), NEW_YORK)
        ))
        connection.execute(insert(CorporateAction), [
            {"instrument_id": ids["XCO"], "source": "alpaca", "source_ref": "split", "kind": "forward_split", "ex_date": SPLIT_DAY,
             "process_date": SPLIT_DAY, "old_rate": 1, "new_rate": 2, "rate": None, "currency": None, "details": {}},
            {"instrument_id": ids["XCO"], "source": "alpaca", "source_ref": "dividend", "kind": "cash_dividend", "ex_date": DIVIDEND_DAY,
             "process_date": DIVIDEND_DAY, "old_rate": None, "new_rate": None, "rate": Decimal("0.5"), "currency": "USD", "details": {}},
        ])
    return {"event_id": event_of(database, candidate), "instruments": ids}


def windows(measurement):
    return {result.window.key: result for result in measurement.windows}


def test_the_reaction_is_measured_against_the_market_and_the_sector(database, market):
    reaction = event_study_service.of_event(database, market["event_id"], NOW)

    assert reaction.event_day == EVENT_DAY
    assert reaction.estimation == (CALENDAR[START - 130], CALENDAR[START - 11])
    assert reaction.period == Period.UNDIVIDED
    assert (reaction.sector.key, reaction.sector.symbol) == ("oil-gas", "XLE")
    [exco] = reaction.reactions
    against = {each.benchmark: each for each in exco.measurements}
    assert {benchmark: each.symbol for benchmark, each in against.items()} == {Benchmark.MARKET: "SPY", Benchmark.SECTOR: "XLE"}

    market_model = against[Benchmark.MARKET]
    # The split and the dividend leave no trace in the returns the model was estimated from.
    assert market_model.model.days == 120
    assert market_model.model.beta == pytest.approx(1.5, abs=0.15)
    day = windows(market_model)["day"]
    assert day.status == Status.DONE
    assert day.abnormal == pytest.approx(JUMP, abs=0.012)
    assert day.t > 3
    assert day.excess == pytest.approx(day.raw - day.benchmark)
    assert windows(market_model)["pre"].status == Status.DONE


def test_adjustments_and_gaps_are_named_never_filled(database, market):
    [exco] = event_study_service.of_event(database, market["event_id"], NOW).reactions

    texts = {(each.symbol, each.day, each.text) for each in exco.adjustments}
    assert ("XCO", SPLIT_DAY, "Aktiensplit 1:2") in texts
    assert ("XCO", DIVIDEND_DAY, "Dividende 0,5 USD") in texts
    assert exco.gaps == [GAP_DAY]
    market_model = windows(exco.measurements[0])
    assert market_model["d1"].status == Status.DONE
    assert (market_model["d5"].status, market_model["d5"].missing) == (Status.GAP, [("XCO", GAP_DAY)])
    assert market_model["d5"].abnormal is None
    # Ends 9 July: not closed on 1 July.
    assert market_model["d20"].status == Status.OPEN


def test_prices_not_fetched_yet_are_pending(database, market):
    with database.begin() as connection:
        connection.execute(update(Instrument).where(Instrument.symbol == "SPY").values(history_through=None))
        connection.execute(delete(PricePoint).where(PricePoint.instrument_id == market["instruments"]["SPY"]))
    [exco] = event_study_service.of_event(database, market["event_id"], NOW).reactions
    market_model = exco.measurements[0]
    assert market_model.pending and market_model.model is None
    assert windows(market_model)["day"].status == Status.PENDING


def test_the_overview_counts_what_was_measured_and_says_why_the_rest_was_not(database, market):
    overview = event_study_service.overview(database, Benchmark.MARKET, Measure.MODEL, NOW)

    assert (overview.events, overview.observations, overview.periods) == (1, 1, [Period.UNDIVIDED])
    figures = {(row.event_type, row.window.key): row.figures[Period.UNDIVIDED] for row in overview.rows}
    assert figures[(None, "day")].count == 1
    assert figures[(None, "day")].mean == pytest.approx(JUMP, abs=0.012)
    assert figures[(STRIKE["event_type"], "day")].count == 1
    assert dict(figures[(None, "d5")].excluded) == {Exclusion.GAP: 1}
    assert dict(figures[(None, "d60")].excluded) == {Exclusion.OPEN: 1}

    sector = event_study_service.overview(database, Benchmark.SECTOR, Measure.EXCESS, NOW)
    assert {(row.event_type, row.window.key): row.figures[Period.UNDIVIDED] for row in sector.rows}[(None, "day")].count == 1


def test_a_window_reaching_past_the_end_of_its_period_does_not_count_there(database, market):
    platform_settings_service.save(database, {"study_training_until": EVENT_DAY.isoformat()})

    overview = event_study_service.overview(database, Benchmark.MARKET, Measure.EXCESS, NOW)

    assert overview.periods == [Period.TRAINING, Period.TEST]
    figures = {(row.event_type, row.window.key): row.figures for row in overview.rows}
    assert figures[(None, "day")][Period.TRAINING].count == 1
    assert dict(figures[(None, "d1")][Period.TRAINING].excluded) == {Exclusion.CUT: 1}
    assert figures[(None, "day")][Period.TEST].count == 0
    assert event_study_service.of_event(database, market["event_id"], NOW).period == Period.TRAINING


def test_an_event_naming_no_company_with_daily_prices_has_no_reaction(database, market):
    other = event_of(database, reported(database, "y", QUAKE, published_at=REPORTED_AT))
    reaction = event_study_service.of_event(database, other, NOW)
    assert reaction.reactions == [] and reaction.sector is None


def test_the_benchmarks_prices_are_fetched_first(database):
    with database.begin() as connection:
        others = [instrument(connection, symbol) for symbol in ("AAA", "BBB")]
        spy = instrument(connection, "SPY", "etf")
        connection.execute(update(Instrument).values(history_through=None))
        due = instrument_repository.history_due(connection, "alpaca", LAST_FETCHED, ("SPY",))
    assert [row.id for row in due] == [spy, *others]
