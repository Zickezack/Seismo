"""The Prognosen's rules without a database (forecast_service): what a measured window makes of an outcome, and how
a forecast reads."""
from datetime import UTC, date, datetime

import pytest

from app.services import forecast_service
from app.services.event_study_service import WINDOWS, Calendar, Status, WindowResult
from app.services.forecast_service import ForecastView, HitRate

DAY = date(2026, 11, 5)


def result(status, excess=None, missing=()):
    return WindowResult(WINDOWS[3], DAY, DAY, status, list(missing), 0.01 if excess is not None else None, 0.03 if excess is not None else None, excess)


@pytest.mark.parametrize(("direction", "excess", "hit"), [("negative", -0.02, True), ("negative", 0.02, False), ("positive", 0.02, True), ("positive", 0.0, False)])
def test_a_hit_is_the_excess_return_the_forecast_way(direction, excess, hit):
    values = forecast_service.outcome(direction, result(Status.DONE, excess))
    assert (values["status"], values["excess_return"], values["hit"]) == ("measured", excess, hit)


def test_open_or_pending_is_not_due_and_a_gap_or_an_end_is_stored_unmeasured():
    assert forecast_service.outcome("negative", result(Status.OPEN)) is None
    assert forecast_service.outcome("negative", result(Status.PENDING)) is None
    assert forecast_service.outcome("negative", None) is None
    gap = forecast_service.outcome("negative", result(Status.GAP, missing=[("SHEL", DAY)]))
    assert gap == {"status": "gap", "missing": [{"symbol": "SHEL", "day": "2026-11-05"}]}
    assert forecast_service.outcome("negative", result(Status.ENDED))["status"] == "ended"


def test_the_last_trading_day_closed_at_a_moment():
    days = Calendar([date(2026, 11, 3), date(2026, 11, 4)], [datetime(2026, 11, 3, 21, tzinfo=UTC), datetime(2026, 11, 4, 21, tzinfo=UTC)])
    assert days.last_closed(datetime(2026, 11, 4, 20, tzinfo=UTC)) == 0
    assert days.last_closed(datetime(2026, 11, 4, 21, tzinfo=UTC)) == 1
    assert days.last_closed(datetime(2026, 11, 2, tzinfo=UTC)) is None


def view(**fields):
    values = dict(
        id=1, event_id=7, event_title="Oil", hypothesis_id=3, target_id=4, target_name="Shell", instrument_id=9, asset_symbol="SHEL",
        asset_name="Shell plc", benchmark_symbol="XLE", benchmark="sector", horizon=20, direction="negative", probability="high",
        classification="evidenced", entry_day=date(2026, 11, 3), end_day=date(2026, 12, 2), prices_through=date(2026, 11, 3),
        origin="llm", model="qwen3.5:9b", prompt_version=3, created_at=datetime(2026, 11, 4, tzinfo=UTC), outcome_status=None,
        asset_return=None, benchmark_return=None, excess_return=None, hit=None, missing=[], recorded_at=None,
    )
    return ForecastView(**values | fields)


def test_a_forecast_reads_as_the_pages_show_it():
    open_one = view()
    assert (open_one.outcome_label, open_one.benchmark_label, open_one.made_by) == ("offen bis 02.12.2026", "Branche (XLE)", "LLM (qwen3.5:9b, Prompt 3)")
    measured = view(outcome_status="measured", asset_return=-0.05, benchmark_return=0.01, excess_return=-0.06, hit=True)
    assert measured.outcome_label == "-6,00 % gegen XLE: Treffer"
    assert measured.returns_label == "SHEL -5,00 %, XLE +1,00 %"
    gap = view(outcome_status="gap", missing=[{"symbol": "SHEL", "day": "2026-11-20"}])
    assert (gap.outcome_label, gap.missing_label) == ("nicht messbar: Lücke", "ohne Kurs: SHEL 20.11.2026")
    assert view(origin="analyst", model=None, prompt_version=None).made_by == "Analyst"


def test_a_hit_rate_reads_as_counts():
    rate = HitRate(20, "medium", 3, 2, 0.0123)
    assert (rate.probability_label, rate.rate_label, rate.mean_label) == ("mittel", "67 %", "+1,23 %")
