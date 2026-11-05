"""Prognosen in PostgreSQL (userstories.md, Prognoseprotokoll und Evaluation, story 1): logged with a version of a
Wirkungsannahme, never changed - the database refuses it -, measured from the last close before it, the outcome
stored apart once its horizon closed and its prices are in."""
import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.models import EntityAlias, EntityMention, Forecast, ForecastOutcome, Instrument, PricePoint, TradingDay
from app.services import forecast_evaluation_service, forecast_service, impact_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_context import event_of, reported, statement
from tests.integration.test_event_filter import named
from tests.integration.test_event_study import NEW_YORK
from tests.integration.test_impacts import REPORT, answer, assess, hypothesis

TODAY = datetime.now(UTC).date()
CALENDAR = [TODAY - timedelta(days=300) + timedelta(days=n) for n in range(460) if (TODAY - timedelta(days=300) + timedelta(days=n)).weekday() < 5]
# Synthetic prices reach into the future: an outcome is measured by moving the clock there.
FETCHED = CALENDAR[-20]
LATER = datetime.combine(FETCHED + timedelta(days=1), time(), UTC)  # all three horizons closed and fetched
DROP = -0.004  # Shell against the oil ETF per day after the forecast


def instrument(connection, symbol, kind="stock", entity_id=None):
    values = {
        "source": "alpaca", "source_ref": f"id-{symbol.lower()}", "symbol": symbol, "exchange": "NYSE", "name": f"{symbol} Inc.",
        "kind": kind, "currency": "USD", "history": True, "history_through": FETCHED, "entity_id": entity_id, "live": False,
    }
    return connection.execute(insert(Instrument).values(values).returning(Instrument.id)).scalar_one()


def store(connection, instrument_id, returns):
    rows, price = [], 100.0
    for index, day in enumerate(CALENDAR):
        if day > FETCHED:
            break
        if index:
            price *= 1 + returns[index]
        rows.append({"instrument_id": instrument_id, "resolution": "day", "at": datetime.combine(day, time(), NEW_YORK), "open": price,
                     "high": price, "low": price, "close": Decimal(f"{price:.6f}"), "volume": 1, "source": "alpaca", "delayed": False})
    connection.execute(insert(PricePoint), rows)


@pytest.fixture
def oil(database):
    """Shell, named by an oil event that names the sector Öl und Gas; SPY, the oil ETF XLE and Shell's Wertpapier SHEL,
    which falls against XLE after today."""
    candidate = reported(database, "oil", REPORT, [statement("source_forecast", "Freight rates stay high.")])
    shell = named(database, candidate, "company", "Shell")
    oil_sector = named(database, candidate, "sector", "Öl und Gas", position=1)
    total = named(database, candidate, "company", "TotalEnergies", position=2)
    with database.begin() as connection:
        alias = connection.execute(select(EntityAlias.id).where(EntityAlias.entity_id == shell)).scalar()
        connection.execute(insert(EntityMention).values(candidate_id=candidate, alias_id=alias, source="impact", position=0))
        connection.execute(insert(EntityAlias).values(entity_id=oil_sector, scheme="catalogue", value="oil-gas", key="oil-gas", match="founding"))
        connection.execute(insert(TradingDay), [
            {"day": day, "opens_at": datetime.combine(day, time(9, 30), NEW_YORK), "closes_at": datetime.combine(day, time(16), NEW_YORK)}
            for day in CALENDAR
        ])
        rng = random.Random(5)
        market = [rng.gauss(0.0004, 0.01) for _ in CALENDAR]
        oil_etf = [0.8 * each + rng.gauss(0, 0.005) for each in market]
        shell_returns = [each + rng.gauss(0, 0.001) + (DROP if day > TODAY else 0) for each, day in zip(oil_etf, CALENDAR, strict=True)]
        ids = {"SPY": instrument(connection, "SPY", "etf"), "XLE": instrument(connection, "XLE", "etf"),
               "SHEL": instrument(connection, "SHEL", entity_id=shell)}
        store(connection, ids["SPY"], market)
        store(connection, ids["XLE"], oil_etf)
        store(connection, ids["SHEL"], shell_returns)
    return {"event_id": event_of(database, candidate), "shell": shell, "total": total, "ids": ids}


SAYS = {"probability": "high", "abstention": "", "expected": "no", "expected_support": []}
# The targets by name, as the ICU collation sorts them: Öl und Gas (Ö as O), Shell, TotalEnergies.
SHELL, TOTAL = 2, 3


def forecasts(database):
    with database.connect() as connection:
        return connection.execute(select(Forecast).order_by(Forecast.id)).all()


def shell_hypothesis(database, oil):
    return next(each for each in impact_service.of_event(database, oil["event_id"]).hypotheses if each.target_name == "Shell")


def test_a_version_with_a_probability_logs_a_forecast_per_horizon(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))

    logged = forecasts(database)
    assert [each.horizon for each in logged] == [5, 20, 60]
    first = logged[0]
    assert (first.instrument_id, first.benchmark_instrument_id, first.benchmark, first.sector_key) == (
        oil["ids"]["SHEL"], oil["ids"]["XLE"], "sector", "oil-gas",
    )
    assert (first.direction, first.probability, first.classification, first.origin, first.model) == (
        "negative", "high", "evidenced", "llm", "qwen3.5:9b",
    )
    # The last trading day closed when it was logged: the whole window after it.
    closed = max(day for day in CALENDAR if datetime.combine(day, time(16), NEW_YORK) <= first.created_at)
    assert first.entry_day == closed and first.end_day == CALENDAR[CALENDAR.index(closed) + 5]
    assert first.prices_through == FETCHED and first.metric == "excess_return"


def test_a_confirmation_repeats_it_and_logs_none_a_change_of_probability_does(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    shell = shell_hypothesis(database, oil)

    impact_service.confirm(database, oil["event_id"], [shell.id])
    assert len(forecasts(database)) == 3
    form = {"direction": "negative", "horizon": "months", "mechanism": "Costs.", "counterarguments": "Hedged.", "probability": "low"}
    impact_service.change(database, shell.id, form, ["i" + shell.support[0].key[1:]], [])
    logged = forecasts(database)
    assert len(logged) == 6 and {each.probability for each in logged[3:]} == {"low"} and logged[3].origin == "analyst"


def test_no_forecast_without_a_probability_a_direction_or_a_wertpapier(database, oil):
    assess(database, oil["event_id"], answer(
        hypothesis(SHELL, direction="mixed") | SAYS,
        hypothesis(TOTAL) | SAYS,  # TotalEnergies has no Wertpapier
    ))
    assert forecasts(database) == []
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS | {"probability": None, "abstention": "Unclear."}))
    assert forecasts(database) == []


def test_the_database_refuses_to_change_or_delete_a_forecast(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    for statement_ in (update(Forecast).values(probability="low"), delete(Forecast), text("TRUNCATE forecast CASCADE")):
        with pytest.raises(DBAPIError, match="cannot be changed once logged"), database.begin() as connection:
            connection.execute(statement_)
    assert {each.probability for each in forecasts(database)} == {"high"}


def test_the_outcome_is_stored_apart_once_its_horizon_closed_and_its_prices_are_in(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    five, twenty, sixty = forecasts(database)

    assert forecast_service.resolve_due(database, datetime.combine(five.end_day, time(12), UTC)) == 0  # not closed yet
    assert forecast_service.resolve_due(database, LATER) == 3
    with database.connect() as connection:
        outcomes = {row.forecast_id: row for row in connection.execute(select(ForecastOutcome))}
    assert set(outcomes) == {five.id, twenty.id, sixty.id}
    outcome = outcomes[twenty.id]
    assert outcome.status == "measured" and outcome.hit is True
    assert outcome.excess_return == pytest.approx(outcome.asset_return - outcome.benchmark_return)
    assert outcome.excess_return == pytest.approx((1 + DROP) ** 20 - 1, abs=0.02)
    assert forecast_service.resolve_due(database, LATER) == 0  # once

    view = forecast_service.overview(database)
    assert (view.open, view.measured, view.unmeasured) == (0, 3, 0)
    assert [(rate.horizon, rate.probability, rate.count, rate.hits) for rate in view.rates] == [
        (5, "high", 1, 1), (20, "high", 1, 1), (60, "high", 1, 1),
    ]
    assert forecast_service.overview(database, "open").forecasts == []
    assert len(forecast_service.overview(database, "resolved").forecasts) == 3


def test_a_missing_close_makes_it_not_measurable_never_filled(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    five, twenty, _ = forecasts(database)
    gap = CALENDAR[CALENDAR.index(five.end_day) + 3]
    with database.begin() as connection:
        connection.execute(delete(PricePoint).where(
            PricePoint.instrument_id == oil["ids"]["SHEL"], PricePoint.at == datetime.combine(gap, time(), NEW_YORK)
        ))

    forecast_service.resolve_due(database, LATER)

    [view] = [each for each in forecast_service.of_hypothesis(database, shell_hypothesis(database, oil).id) if each.id == twenty.id]
    assert (view.outcome_status, view.excess_return, view.hit) == ("gap", None, None)
    assert view.missing_label == f"ohne Kurs: SHEL {gap.strftime('%d.%m.%Y')}"
    assert view.outcome_label == "nicht messbar: Lücke"


def test_an_outcome_waits_for_the_benchmarks_prices(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    with database.begin() as connection:
        connection.execute(update(Instrument).where(Instrument.symbol == "XLE").values(history_through=TODAY))
    assert forecast_service.resolve_due(database, LATER) == 0
    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ForecastOutcome)).scalar_one() == 0


def test_the_hit_rate_is_broken_down_by_what_may_explain_it(database, oil):
    """Story 3: of one horizon, per model and prompt, event type, classification, way, sector, probability and month."""
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    forecast_service.resolve_due(database, LATER)

    evaluation = forecast_evaluation_service.evaluation(database)
    assert evaluation.horizon == "20"
    assert (evaluation.total.count, evaluation.total.events, evaluation.total.hits, evaluation.total.verdict) == (1, 1, 1, "few")
    groups = {each.key: [(group.key, group.label, group.count) for group in each.groups] for each in evaluation.breakdowns}
    assert "horizon" not in groups
    assert groups["made_by"] == [("llm:qwen3.5:9b:3", "LLM (qwen3.5:9b, Prompt 3)", 1)]
    assert groups["classification"] == [("evidenced", "belegt", 1)]
    assert groups["way"] == [("direct", "direkt", 1)]
    assert groups["sector"] == [("sector:oil-gas", "Öl und Gas", 1)]
    assert groups["probability"] == [("high", "hoch", 1)]
    [event_type] = groups["event_type"]
    assert event_type[2] == 1
    logged = forecasts(database)[0].created_at.astimezone(UTC)
    assert [month.key for month in evaluation.months] == [f"{logged.year:04d}-{logged.month:02d}"]

    pooled = forecast_evaluation_service.evaluation(database, "all")
    assert (pooled.total.count, pooled.total.events) == (3, 1)
    assert [(each.key, each.count) for each in next(b for b in pooled.breakdowns if b.key == "horizon").groups] == [("5", 1), ("20", 1), ("60", 1)]
    assert forecast_evaluation_service.evaluation(database, "nonsense").horizon == "20"
