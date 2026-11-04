"""The event study's rules without a database (event_study_service): returns with splits and dividends, the market
model, the Ereignistag, a window's state and measures, the periods, and the overview's figures."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import event_study_service as study
from app.services.event_study_service import (
    Benchmark,
    Exclusion,
    Figure,
    Measure,
    Period,
    Series,
    Split,
    Status,
    Window,
)

DAYS = [date(2025, 6, 2) + timedelta(days=n) for n in range(12)]  # every day a trading day, for the arithmetic
CLOSES_AT = [datetime(day.year, day.month, day.day, 20, tzinfo=UTC) for day in DAYS]


def series(closes, history_through=DAYS[-1], delisted_on=None, symbol="XCO"):
    closes = dict(zip(DAYS, map(Decimal, closes))) if isinstance(closes, list) else closes
    return Series(1, symbol, symbol, history_through, delisted_on, closes, study.daily_returns(DAYS, closes, [], {}))


# Returns


def test_a_return_is_from_close_to_close():
    returns = study.daily_returns(DAYS[:3], {DAYS[0]: Decimal(100), DAYS[1]: Decimal(110), DAYS[2]: Decimal(99)}, [], {})
    assert returns == {1: pytest.approx(0.10), 2: pytest.approx(-0.10)}


def test_a_split_does_not_count_as_a_return():
    # One share into two on the second day: the price halves, the holder's value does not.
    closes = {DAYS[0]: Decimal(100), DAYS[1]: Decimal(51), DAYS[2]: Decimal("51.51")}
    returns = study.daily_returns(DAYS[:3], closes, [Split(DAYS[1], Decimal(1) / Decimal(2))], {})
    assert returns == {1: pytest.approx(0.02), 2: pytest.approx(0.01)}


def test_a_dividend_counts_on_its_ex_date():
    # The price falls by the dividend going ex: the holder got it.
    closes = {DAYS[0]: Decimal(100), DAYS[1]: Decimal(99)}
    assert study.daily_returns(DAYS[:2], closes, [], {DAYS[1]: Decimal(1)}) == {1: pytest.approx(0.0)}


def test_a_day_after_a_missing_close_has_no_return():
    closes = {DAYS[0]: Decimal(100), DAYS[2]: Decimal(101), DAYS[3]: Decimal(102)}
    assert set(study.daily_returns(DAYS[:4], closes, [], {})) == {3}


# The market model


def test_the_market_model_finds_alpha_and_beta():
    market = {index: ((index * 7) % 11 - 5) / 1000 for index in range(1, 81)}
    stock = {index: 0.0005 + 1.5 * value + ((index * 3) % 5 - 2) / 10000 for index, value in market.items()}
    model = study.market_model(stock, market, range(1, 81))
    assert model.days == 80
    assert model.beta == pytest.approx(1.5, abs=0.02)
    assert model.alpha == pytest.approx(0.0005, abs=0.0002)
    assert model.sigma > 0


def test_too_few_days_give_no_model():
    market = {index: index / 1000 for index in range(study.MIN_ESTIMATION_DAYS - 1)}
    assert study.market_model(market, market, range(100)) is None


def test_a_benchmark_that_never_moves_gives_no_model():
    flat = {index: 0.0 for index in range(100)}
    assert study.market_model(flat, flat, range(100)) is None


# The Ereignistag


def test_the_ereignistag_is_the_first_trading_day_closing_after_the_first_report():
    assert study.event_index(CLOSES_AT[3] - timedelta(hours=5), CLOSES_AT) == 3  # during the session
    assert study.event_index(CLOSES_AT[3], CLOSES_AT) == 4  # at the close: the next day prices it
    assert study.event_index(CLOSES_AT[3] + timedelta(hours=2), CLOSES_AT) == 4  # after the close
    assert study.event_index(CLOSES_AT[-1] + timedelta(hours=1), CLOSES_AT) is None  # beyond the calendar


# A window


NOW = CLOSES_AT[-1] + timedelta(hours=1)
WINDOW = Window("d2", "bis +2", 0, 2)


def test_a_window_reads_the_close_before_its_first_day():
    prices = [100, 100, 100, 100, 110, 110, 121, 121, 121, 121, 121, 121]
    flat = series([100] * 12, symbol="SPY")
    result = study.measure_window(WINDOW, 4, series(prices), flat, None, DAYS, CLOSES_AT, NOW, None)
    assert result.status == Status.DONE
    assert (result.first_day, result.last_day) == (DAYS[4], DAYS[6])
    assert result.raw == pytest.approx(0.21)
    assert result.benchmark == pytest.approx(0.0)
    assert result.excess == pytest.approx(0.21)
    assert result.abnormal is None and result.t is None


def test_the_abnormal_return_sums_what_the_model_does_not_explain():
    stock, market = series([100] * 4 + [110] * 8), series([100] * 4 + [105] * 8, symbol="SPY")
    model = study.Model(alpha=0.0, beta=1.0, sigma=0.01, days=100)
    result = study.measure_window(WINDOW, 4, stock, market, model, DAYS, CLOSES_AT, NOW, None)
    assert result.abnormal == pytest.approx(0.05)
    assert result.t == pytest.approx(0.05 / (0.01 * 3 ** 0.5))


def test_a_missing_close_is_a_gap_named_never_filled():
    closes = dict(zip(DAYS, map(Decimal, [100] * 12)))
    del closes[DAYS[5]]
    result = study.measure_window(WINDOW, 4, series(closes), series([100] * 12, symbol="SPY"), None, DAYS, CLOSES_AT, NOW, None)
    assert result.status == Status.GAP
    assert result.missing == [("XCO", DAYS[5])]
    assert result.raw is None and result.excess is None


def test_a_window_not_closed_yet_is_open():
    now = CLOSES_AT[5]  # day 6's close is still to come
    result = study.measure_window(WINDOW, 4, series([100] * 12), series([100] * 12, symbol="SPY"), None, DAYS, CLOSES_AT, now, None)
    assert result.status == Status.OPEN


def test_prices_not_fetched_yet_are_pending_and_after_the_last_trading_day_ended():
    closes = dict(zip(DAYS[:5], map(Decimal, [100] * 5)))
    market = series([100] * 12, symbol="SPY")
    pending = study.measure_window(WINDOW, 4, series(closes, history_through=DAYS[4]), market, None, DAYS, CLOSES_AT, NOW, None)
    assert pending.status == Status.PENDING
    ended = study.measure_window(WINDOW, 4, series(closes, delisted_on=DAYS[4]), market, None, DAYS, CLOSES_AT, NOW, None)
    assert ended.status == Status.ENDED
    # The benchmark's state counts as much as the Wertpapier's.
    result = study.measure_window(WINDOW, 4, series([100] * 12), series(closes, history_through=None, symbol="SPY"), None, DAYS, CLOSES_AT, NOW, None)
    assert result.status == Status.PENDING
    assert ("SPY", DAYS[5]) in result.missing


def test_a_window_past_its_periods_end_is_cut():
    flat = series([100] * 12, symbol="SPY")
    assert study.measure_window(WINDOW, 4, series([100] * 12), flat, None, DAYS, CLOSES_AT, NOW, DAYS[6]).cut is False
    assert study.measure_window(WINDOW, 4, series([100] * 12), flat, None, DAYS, CLOSES_AT, NOW, DAYS[5]).cut is True


# Periods


TRAINING, VALIDATION = date(2025, 12, 31), date(2026, 6, 30)


@pytest.mark.parametrize(
    ("day", "training", "validation", "period"),
    [
        (date(2025, 1, 1), None, None, Period.UNDIVIDED),
        (TRAINING, TRAINING, VALIDATION, Period.TRAINING),
        (TRAINING + timedelta(days=1), TRAINING, VALIDATION, Period.VALIDATION),
        (VALIDATION, TRAINING, VALIDATION, Period.VALIDATION),
        (VALIDATION + timedelta(days=1), TRAINING, VALIDATION, Period.TEST),
        (TRAINING + timedelta(days=1), TRAINING, None, Period.TEST),
    ],
)
def test_an_event_belongs_to_a_period_by_its_first_reports_day(day, training, validation, period):
    assert study.period_of(day, training, validation) == period


def test_only_training_and_validation_end():
    assert study.period_end(Period.TRAINING, TRAINING, VALIDATION) == TRAINING
    assert study.period_end(Period.VALIDATION, TRAINING, VALIDATION) == VALIDATION
    assert study.period_end(Period.TEST, TRAINING, VALIDATION) is None
    assert study.period_end(Period.UNDIVIDED, None, None) is None


# The overview


def test_a_figure_shows_count_mean_median_and_share_above_zero():
    figure = Figure([0.03, -0.01, 0.01])
    figure.excluded[Exclusion.GAP] += 2
    assert (figure.count, figure.mean, figure.median, figure.positive) == (3, pytest.approx(0.01), 0.01, pytest.approx(2 / 3))
    assert figure.excluded_count == 2
    assert figure.excluded_label == "Lücke 2"
    assert Figure().mean is None and Figure().positive is None


def test_what_does_not_count_says_why():
    done = study.WindowResult(WINDOW, DAYS[0], DAYS[2], Status.DONE, [], 0.1, 0.05, 0.05, None, None)
    assert study._exclusion(done, Measure.EXCESS) is None
    assert study._exclusion(done, Measure.MODEL) == Exclusion.NO_MODEL
    cut = study.WindowResult(WINDOW, DAYS[0], DAYS[2], Status.DONE, [], 0.1, 0.05, 0.05, 0.04, 2.0, cut=True)
    assert study._exclusion(cut, Measure.MODEL) == Exclusion.CUT
    gap = study.WindowResult(WINDOW, DAYS[0], DAYS[2], Status.GAP, [("XCO", DAYS[1])], cut=True)
    assert study._exclusion(gap, Measure.MODEL) == Exclusion.GAP


def test_the_choice_comes_from_the_url_with_defaults():
    assert study.parse_choice({}) == (Benchmark.MARKET, Measure.MODEL)
    assert study.parse_choice({"benchmark": "sector", "measure": "excess"}) == (Benchmark.SECTOR, Measure.EXCESS)
    assert study.parse_choice({"benchmark": "x'; drop", "measure": ""}) == (Benchmark.MARKET, Measure.MODEL)


def test_labels():
    assert study.percent_label(0.0123) == "+1,23 %"
    assert study.percent_label(-0.05) == "-5,00 %"
    assert study.percent_label(0.00001) == "±0,00 %"
    assert study.percent_label(None) == "–"
    assert study.share_label(2 / 3) == "67 %"
    assert study.number_label(-2.345) == "-2,35"
