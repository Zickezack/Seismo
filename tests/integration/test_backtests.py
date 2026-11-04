"""Backtests in PostgreSQL (userstories.md, Prognoseprotokoll und Evaluation, story 2): a run asked for, computed by
the worker's job from the data received by its data state, stored for good with its trades, run again to the same
result, and judged on the test period with every variant counted."""
from bisect import bisect_right
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.models import BacktestRun, BacktestTrade, Forecast, Instrument, Job, PricePoint
from app.services import backtest_service, platform_settings_service
from app.services.backtest_service import InvalidBacktest, threshold
from app.services.job_service import RUN_BACKTEST_JOB_KIND
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_study import NEW_YORK
from tests.integration.test_forecasts import CALENDAR, DROP, LATER, SAYS, SHELL, TODAY, oil  # noqa: F401
from tests.integration.test_impacts import answer, assess, hypothesis
from tests.integration.test_ingestion import run_jobs

FORM = {"mode": "created", "horizon": "20", "min_probability": "low", "model": "", "backtest_cost_bps": "10", "backtest_borrow_bps": "50"}
OPENS = [datetime.combine(day, time(9, 30), NEW_YORK) for day in CALENDAR]


def backtest(database, form=FORM, now=LATER):
    """Ask for a run, let the worker compute it; the new run's id."""
    backtest_service.start(database, form, now)
    assert run_jobs(database, RUN_BACKTEST_JOB_KIND) == 1
    return latest(database)


def latest(database):
    with database.connect() as connection:
        return connection.execute(select(BacktestRun.id).order_by(BacktestRun.id.desc()).limit(1)).scalar_one()


def trades(database, run_id):
    with database.connect() as connection:
        return connection.execute(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.id)).all()


@pytest.fixture
def assessed(database, oil):
    assess(database, oil["event_id"], answer(hypothesis(SHELL) | SAYS))
    with database.connect() as connection:
        logged = connection.execute(select(Forecast).where(Forecast.horizon == 20)).one()
    return oil | {"forecast": logged}


def test_a_run_trades_each_signal_from_the_next_open_against_the_sector_and_paper_trades_the_forecasts(database, assessed):
    run_id = backtest(database)

    signal, paper = trades(database, run_id)
    assert (signal.kind, paper.kind, paper.forecast_id) == ("signal", "paper", assessed["forecast"].id)
    assert (signal.instrument_id, signal.benchmark_instrument_id) == (assessed["ids"]["SHEL"], assessed["ids"]["XLE"])
    entry = bisect_right(OPENS, signal.signal_at)
    assert (signal.entry_day, signal.exit_day) == (CALENDAR[entry], CALENDAR[entry + 19])
    assert signal.status == "measured" and signal.direction == "negative"
    # From the entry's open - the synthetic open is its close - Shell falls against the oil ETF day by day.
    assert signal.asset_return - signal.benchmark_return == pytest.approx((1 + DROP) ** 19 - 1, abs=0.02)
    assert signal.trend in ("up", "down") and signal.period == "undivided"

    run = backtest_service.detail(database, run_id)
    assert run.counts == {"signals": 1, "measured": 1, "open": 0, "unmeasured": 0, "paper": 1}
    by_key = {each.key: each for each in run.segments}
    assert (by_key["all"].model.count, by_key["all"].model.hits, by_key["paper"].model.count) == (1, 1, 1)
    assert by_key["all"].model.mean == pytest.approx(signal.benchmark_return - signal.asset_return - run.parameters.cost)
    assert by_key["all"].figures["long"].hits == 0
    assert run.verdict.state == "in_sample" and not run.verdict.proven
    assert run.as_of == LATER and run.parameters == backtest_service.parse(FORM)


def test_a_run_sees_only_what_was_received_by_its_data_state(database, assessed):
    now = datetime.now(UTC)
    open_run = backtest(database, now=now)
    assert {each.status for each in trades(database, open_run)} == {"open"}  # its last day closes later

    signal = trades(database, backtest(database))[0]
    missing = CALENDAR[CALENDAR.index(signal.entry_day) + 3]
    at = datetime.combine(missing, time(), NEW_YORK)
    with database.begin() as connection:
        row = connection.execute(select(PricePoint.__table__).where(PricePoint.instrument_id == assessed["ids"]["SHEL"], PricePoint.at == at)).one()
        connection.execute(delete(PricePoint).where(PricePoint.id == row.id))
        # Received only after the data state: as if it came late.
        late = {key: value for key, value in row._mapping.items() if key != "id"}
        connection.execute(insert(PricePoint).values(late | {"received_at": LATER + timedelta(days=1)}))
    late = trades(database, backtest(database))[0]
    assert late.status == "gap" and late.missing == [{"symbol": "SHEL", "day": missing.isoformat()}]


def test_a_model_or_probability_left_out_trades_nothing(database, assessed):
    assert [each.kind for each in trades(database, backtest(database, FORM | {"model": "qwen3.5:9b|3"}))] == ["signal", "paper"]
    assert trades(database, backtest(database, FORM | {"model": "other|1"})) == []


def test_the_database_refuses_to_change_or_delete_a_run(database, assessed):
    run_id = backtest(database)
    for statement in (
        update(BacktestRun).values(cost_bps=0), delete(BacktestTrade), delete(BacktestRun),
        text("TRUNCATE backtest_trade"), text("TRUNCATE backtest_run CASCADE"),
    ):
        with pytest.raises(DBAPIError, match="cannot be changed once stored"), database.begin() as connection:
            connection.execute(statement)
    assert len(trades(database, run_id)) == 2


def test_a_run_again_comes_out_the_same_or_names_how_many_trades_differ(database, assessed):
    first = backtest(database)
    backtest_service.reproduce(database, first)
    run_jobs(database, RUN_BACKTEST_JOB_KIND)
    again = backtest_service.detail(database, latest(database))
    assert (again.reproduces_id, again.reproduced, again.as_of) == (first, True, LATER)

    # Which Wertpapier belongs to a target is read as now: a correction since shows as a difference.
    with database.begin() as connection:
        connection.execute(update(Instrument).where(Instrument.symbol == "SHEL").values(entity_id=None))
    backtest_service.reproduce(database, first)
    run_jobs(database, RUN_BACKTEST_JOB_KIND)
    changed = backtest_service.detail(database, latest(database))
    assert (changed.reproduced, changed.differing) == (False, 1)
    assert backtest_service.reproduce(database, 999) is None


def test_every_variant_tried_on_the_test_period_raises_the_threshold(database, assessed):
    platform_settings_service.save(database, {"study_training_until": (TODAY - timedelta(days=30)).isoformat()})
    backtest(database)
    run = backtest_service.detail(database, backtest(database, FORM | {"backtest_cost_bps": "5"}))
    assert run.variants == 2 and run.threshold == threshold(2)
    assert any(each.startswith("Mehrfachtest: Test-Perioden wurden in 2 Varianten") for each in run.warnings)
    assert run.verdict.state == "too_few" and run.verdict.text == "Zu wenige Trades in der Test-Periode: 1, mindestens 30."
    # The same variant again with a later data state is no new one.
    assert backtest_service.detail(database, backtest(database, now=LATER + timedelta(days=1))).variants == 2

    platform_settings_service.save(database, {"study_training_until": (TODAY - timedelta(days=60)).isoformat()})
    moved = backtest_service.detail(database, backtest(database))
    assert any(each.startswith("Perioden geändert: Die Läufe nutzten 2 verschiedene Aufteilungen") for each in moved.warnings)


def test_signals_from_the_event_day_are_flagged_as_leakage_and_never_judged(database, assessed):
    run = backtest_service.detail(database, backtest(database, FORM | {"mode": "event_day"}))
    assert run.verdict.state == "leakage"
    assert run.warnings[0].startswith("Datenleckage")
    signal = next(each for each in run.trades if each.kind == "signal")
    with database.connect() as connection:
        first_report = connection.execute(text("SELECT first_reported_at FROM event WHERE id = :id"), {"id": assessed["event_id"]}).scalar_one()
    assert signal.signal_at == first_report


def test_invalid_parameters_ask_for_no_run(database):
    with pytest.raises(InvalidBacktest, match="Horizont"):
        backtest_service.start(database, FORM | {"horizon": "7"})
    with database.connect() as connection:
        assert connection.execute(select(Job.id).where(Job.kind == RUN_BACKTEST_JOB_KIND)).all() == []
