"""The backtests' rules without a database (backtest_service): parameters, returns from the next open, the market
phase, the baselines, the figures and the verdict."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import backtest_service
from app.services.backtest_service import (
    MIN_TRADES,
    InvalidBacktest,
    Parameters,
    TradeView,
    _Prices,
    _Signal,
    calibration,
    figures,
    holding_return,
    parse,
    phase,
    random_direction,
    segments,
    signals,
    strategy_direction,
    threshold,
    trades_hash,
    verdict,
)
from app.services.event_study_service import Split

FORM = {"mode": "created", "horizon": "20", "min_probability": "low", "model": "", "backtest_cost_bps": "10", "backtest_borrow_bps": "50"}
DAYS = [date(2026, 1, 5) + timedelta(days=n) for n in range(10)]


def test_the_form_gives_the_parameters_and_names_the_first_that_does_not_hold():
    assert parse(FORM) == Parameters("created", 20, "low", None, None, 10, 50)
    assert parse(FORM | {"model": "qwen3.5:9b|3"}).model == "qwen3.5:9b" and parse(FORM | {"model": "qwen3.5:9b|3"}).prompt_version == 3
    for change, message in (
        ({"mode": "later"}, "Modus"), ({"horizon": "10"}, "Horizont"), ({"horizon": "x"}, "Horizont"),
        ({"min_probability": "sure"}, "Wahrscheinlichkeit"), ({"model": "qwen"}, "Modell"),
        ({"backtest_cost_bps": "501"}, "Handelskosten"), ({"backtest_borrow_bps": "-1"}, "Leihgebühr"),
    ):
        with pytest.raises(InvalidBacktest, match=message):
            parse(FORM | change)


def test_the_costs_are_both_legs_bought_and_sold_and_the_short_leg_borrowed():
    parameters = Parameters("created", 63, "low", None, None, 10, 100)
    assert parameters.cost == pytest.approx(4 * 0.001 + 0.01 * 63 / 252)
    assert parameters.cost_label == "10 bp je Seite und Bein, Leihe 100 bp pro Jahr (-0,65 % je Trade)"


def test_a_variant_is_everything_but_the_data_state():
    parameters = parse(FORM)
    same = parameters.variant(date(2026, 1, 1), None)
    assert same == parse(dict(FORM)).variant(date(2026, 1, 1), None)
    assert same != parameters.variant(date(2026, 2, 1), None)
    assert same != parse(FORM | {"backtest_cost_bps": "11"}).variant(date(2026, 1, 1), None)


def prices(closes, opens=None, splits=(), dividends=None, reached=None, delisted_on=None):
    closes = {DAYS[index]: Decimal(str(value)) for index, value in closes.items()}
    opens = {DAYS[index]: Decimal(str(value)) for index, value in (opens or {}).items()}
    return _Prices("XCO", delisted_on, reached or DAYS[-1], opens, closes, list(splits), dividends or {})


def test_a_holding_runs_from_the_entry_open_to_the_exit_close_with_dividends_after_the_entry_and_splits():
    stock = prices({1: 100, 2: 102, 3: 51.5}, opens={1: 98}, splits=[Split(DAYS[3], Decimal("0.5"))],
                   dividends={DAYS[1]: Decimal("1"), DAYS[3]: Decimal("0.5")})
    value, missing = holding_return(stock, DAYS, 1, 3)
    # Open 98 to close 100; 100 to 102 (the dividend on the entry day is not the buyer's); then a split 2:1 with 0.5.
    assert missing == [] and value == pytest.approx(100 / 98 * 102 / 100 * (52 * 2) / 102 - 1)


def test_a_missing_open_or_close_is_named_never_filled_and_tells_gap_from_not_yet_in():
    value, missing = holding_return(prices({1: 100, 3: 101}, opens={1: 100}), DAYS, 1, 3)
    assert value is None and missing == [DAYS[2]]
    value, missing = holding_return(prices({1: 100, 2: 100}), DAYS, 1, 2)
    assert missing == [DAYS[1]]
    assert backtest_service._missing_status(prices({}, reached=DAYS[5]), [DAYS[2]]) == "gap"
    assert backtest_service._missing_status(prices({}, reached=DAYS[1]), [DAYS[2]]) == "open"
    assert backtest_service._missing_status(prices({}, reached=DAYS[5], delisted_on=DAYS[1]), [DAYS[2]]) == "ended"


def test_the_market_phase_comes_from_spys_closes_before_the_entry():
    days = [date(2024, 1, 1) + timedelta(days=n) for n in range(400)]
    rising = {day: Decimal(100 + n * 0.1 + (n % 2) * (2 if n > 380 else 0.2)) for n, day in enumerate(days)}
    market = _Prices("SPY", None, days[-1], {}, rising, [], {})
    assert phase(market, days, 399) == ("up", "volatile")
    falling = {day: Decimal(200 - n * 0.1 + (n % 2) * (2 if 120 < n < 330 else 0.2)) for n, day in enumerate(days)}
    assert phase(_Prices("SPY", None, days[-1], {}, falling, [], {}), days, 399) == ("down", "calm")
    # Too few closes before it: unknown, not guessed.
    assert phase(market, days, 100) == (None, None)
    assert phase(None, days, 399) == (None, None)


def row(id, hypothesis_id, direction="negative", probability="high", model="qwen3.5:9b", prompt_version=3, minute=0):
    return SimpleNamespace(
        id=id, hypothesis_id=hypothesis_id, direction=direction, probability=probability, model=model, prompt_version=prompt_version,
        created_at=datetime(2026, 11, 4, 15, minute, tzinfo=UTC), first_reported_at=datetime(2026, 10, 30, 8, tzinfo=UTC),
        event_id=7, target_entity_id=9,
    )


def test_a_version_repeating_the_last_signal_is_none_and_the_filters_apply():
    rows = [row(1, 1), row(2, 1), row(3, 1, probability="low"), row(4, 2, probability="medium", model=None, prompt_version=None)]
    found = signals(rows, parse(FORM))
    assert [each.version_id for each in found] == [1, 3, 4]
    assert found[0].signal_at == rows[0].created_at
    assert [each.version_id for each in signals(rows, parse(FORM | {"min_probability": "medium"}))] == [1, 4]
    assert [each.version_id for each in signals(rows, parse(FORM | {"model": "qwen3.5:9b|3"}))] == [1, 3]
    # From the event's first report: each hypothesis's first signal only, with hindsight.
    early = signals(rows, parse(FORM | {"mode": "event_day"}))
    assert [each.version_id for each in early] == [1, 4] and early[0].signal_at == rows[0].first_reported_at


def test_a_direction_by_chance_is_the_same_for_the_same_trade():
    first = _Signal("signal", 11, None, 1, 7, "negative", "high", datetime(2026, 11, 4, tzinfo=UTC))
    assert random_direction(first) == random_direction(first)
    drawn = {random_direction(_Signal("signal", n, None, 1, 7, "negative", "high", first.signal_at)) for n in range(40)}
    assert drawn == {"positive", "negative"}


def trade(id=1, **values) -> TradeView:
    base = {
        "id": id, "kind": "signal", "hypothesis_id": 1, "event_id": 7, "event_title": "Oil", "target_name": "Shell", "instrument_id": 3,
        "asset_symbol": "SHEL", "benchmark_symbol": "XLE", "direction": "negative", "probability": "high",
        "signal_at": datetime(2026, 11, 4, tzinfo=UTC), "entry_day": date(2026, 11, 5), "exit_day": date(2026, 11, 5) + timedelta(days=id),
        "period": "test", "cut": False, "trend": "up", "volatility": "calm", "status": "measured", "asset_return": -0.03,
        "benchmark_return": 0.01, "momentum": 0.02, "random_direction": "positive", "missing": [],
    }
    return TradeView(**(base | values))


def test_the_strategies_take_their_directions():
    each = trade()
    assert [strategy_direction(name, each) for name in ("model", "long", "random", "momentum")] == ["negative", "positive", "positive", "positive"]
    assert strategy_direction("momentum", trade(momentum=None)) is None


def test_the_figures_count_hits_before_costs_and_returns_after_them_with_the_drawdown_by_exit_day():
    trades = [
        trade(1, asset_return=-0.03, benchmark_return=0.01),  # +4 % in its direction
        trade(3, asset_return=0.02, benchmark_return=0.0),  # -2 %
        trade(2, asset_return=0.01, benchmark_return=0.0),  # -1 %: by exit day before the one above
        trade(4, status="gap", asset_return=None, benchmark_return=None),
    ]
    found = figures(trades, "model", 0.005)
    assert (found.count, found.hits) == (3, 1)
    assert found.mean == pytest.approx((0.035 - 0.015 - 0.025) / 3)
    assert found.total == pytest.approx(-0.005)
    # 3.5 %, then 2 % and -0.5 %: down 4 points from the high.
    assert found.drawdown == pytest.approx(0.04)
    assert found.hit_label == "1 von 3 (33 %)" and found.drawdown_label == "-4,00 %"
    assert figures([], "model", 0.005).hit_label == "–"


def test_the_threshold_rises_with_the_variants_tried():
    assert threshold(1) == pytest.approx(1.96, abs=0.01)
    assert threshold(10) == pytest.approx(2.81, abs=0.01)
    assert threshold(0) == threshold(1)


def test_a_trade_reaching_past_its_periods_end_counts_only_among_all():
    trades = [trade(1, period="training", cut=True), trade(2, period="training"), trade(3, kind="paper", trend=None, volatility=None)]
    by_key = {each.key: each for each in segments(trades, 0.0, True)}
    assert by_key["all"].model.count == 2 and by_key["training"].model.count == 1
    assert by_key["paper"].model.count == 1 and by_key["up"].model.count == 2
    assert "training" not in {each.key for each in segments(trades, 0.0, False)}


def test_the_calibration_tells_whether_hits_rise_with_the_probability():
    rows, rising = calibration([trade(1), trade(2, probability="low", asset_return=0.05)], 0.0, True)
    assert [(row.probability, row.all.count, row.all.hits) for row in rows] == [("high", 1, 1), ("medium", 0, 0), ("low", 1, 0)]
    assert rising is True
    assert calibration([trade(1, probability="high", asset_return=0.05), trade(2, probability="low")], 0.0, True)[1] is False
    assert calibration([trade(1)], 0.0, True)[1] is None


def judged(trades, mode="created", divided=True, variants=1, cost=0.0):
    return verdict(mode, divided, {each.key: each for each in segments(trades, cost, divided)}, variants)


def winners(count, kind="signal"):
    # Short, and right: the Wertpapier falls 3 % against its benchmark, more or less.
    return [trade(n, kind=kind, asset_return=-0.03 - (n % 3) * 0.001, benchmark_return=0.0, momentum=-0.01 + (n % 2) * 0.02,
                  random_direction="negative" if n % 2 else "positive") for n in range(1, count + 1)]


def test_the_verdict_judges_the_test_period_only_and_never_with_hindsight():
    assert judged(winners(MIN_TRADES), mode="event_day").state == "leakage"
    assert judged(winners(MIN_TRADES), divided=False).state == "in_sample"
    assert judged(winners(MIN_TRADES - 1)).state == "too_few"
    training_only = [trade(n, period="training", asset_return=-0.03 - (n % 3) * 0.001) for n in range(1, 40)]
    assert judged(training_only).state == "too_few"

    better = judged(winners(MIN_TRADES) + winners(MIN_TRADES, "paper"))
    assert better.state == "better" and better.proven
    assert better.paper.startswith(f"Paper-Trading: {MIN_TRADES} gemessene Prognosen")
    assert not judged(winners(MIN_TRADES)).proven  # no paper trading yet


def test_the_verdict_names_why_it_is_not_better():
    costly = judged(winners(MIN_TRADES), cost=0.05)
    assert costly.state == "not_better" and "im Mittel" in costly.text
    # Momentum chose the same as the model here: no better than it.
    same = [trade(n, asset_return=-0.03 - (n % 3) * 0.001, benchmark_return=0.0, momentum=-0.01) for n in range(1, MIN_TRADES + 1)]
    assert "nicht besser als «Momentum»" in judged(same).text
    noisy = [trade(n, asset_return=(-0.03 if n % 2 else 0.028), benchmark_return=0.0, momentum=None, random_direction="positive")
             for n in range(1, MIN_TRADES + 1)]
    assert "unter der Schwelle" in judged(noisy, variants=5).text


def test_the_hash_is_the_trades_as_computed():
    rows = [{"kind": "signal", "entry_day": date(2026, 11, 5), "asset_return": 0.1 + 0.2, "signal_at": datetime(2026, 11, 4, tzinfo=UTC)}]
    assert trades_hash(rows) == trades_hash([dict(rows[0])])
    assert trades_hash(rows) != trades_hash([rows[0] | {"asset_return": 0.31}])
