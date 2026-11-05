"""The breakdown of the hit rate without a database (forecast_evaluation_service): the Wilson band, the groups, the
verdict against chance only from enough measured, and the band widened for every group judged."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import backtest_service, forecast_evaluation_service
from app.services.forecast_evaluation_service import MIN_JUDGED, Group, evaluate, wilson


def row(hit=True, signed=0.01, event_id=1, horizon=20, probability="high", classification="evidenced", model="qwen3.5:9b",
        prompt_version=3, sector_key="oil-gas", sector_name="Öl und Gas", event_type="energy_supply", path="direct",
        created_at=datetime(2026, 11, 4, 12, tzinfo=UTC)):
    return SimpleNamespace(
        hit=hit, signed=signed, event_id=event_id, horizon=horizon, probability=probability, classification=classification,
        origin="llm" if model else "analyst", model=model, prompt_version=prompt_version, sector_key=sector_key,
        sector_name=sector_name, event_type=event_type, path=path, created_at=created_at,
    )


def test_the_wilson_band_of_a_share():
    low, high = wilson(18, 30, 1.96)
    assert (round(low, 3), round(high, 3)) == (0.423, 0.754)
    # It stays inside 0 and 1 where the share is at an edge, and is none without a count.
    assert wilson(0, 5, 1.96)[0] == 0.0 and wilson(5, 5, 1.96)[1] == 1.0
    assert wilson(0, 0, 1.96) is None
    # Wider at a higher z.
    assert wilson(18, 30, 2.5)[0] < low


def group(count, hits, low, high):
    return Group("k", "K", count, count, hits, 0.0, low, high)


def test_judged_against_chance_only_from_enough_and_only_when_the_whole_band_is_on_one_side():
    assert group(MIN_JUDGED - 1, MIN_JUDGED - 1, 0.8, 1.0).verdict == "few"
    assert group(MIN_JUDGED, 25, 0.51, 0.9).verdict == "above"
    assert group(MIN_JUDGED, 5, 0.05, 0.49).verdict == "below"
    assert group(MIN_JUDGED, 15, 0.3, 0.7).verdict == "even"
    assert group(MIN_JUDGED, 15, 0.3, 0.7).verdict_label == "nicht unterscheidbar"
    shown = group(40, 30, 0.6, 0.86)
    assert (shown.rate_label, shown.band_label) == ("75 %", "60 % bis 86 %")


def test_groups_per_breakdown_with_their_counts_events_and_mean():
    rows = [
        row(),
        row(hit=False, signed=-0.03, event_id=2, model=None, prompt_version=None, sector_key=None, sector_name=None,
            path="indirect", probability="low", classification="plausible"),
        row(event_id=1, probability="medium", created_at=datetime(2026, 12, 1, tzinfo=UTC)),
    ]
    evaluation = evaluate(rows, "20")
    total = evaluation.total
    assert (total.count, total.events, total.hits) == (3, 2, 2)
    assert total.mean_signed == pytest.approx(-0.01 / 3)
    by_key = {each.key: each for each in evaluation.breakdowns}
    assert list(by_key) == ["made_by", "event_type", "classification", "way", "sector", "probability"]
    assert [(each.label, each.count, each.events) for each in by_key["made_by"].groups] == [
        ("Analyst", 1, 1), ("LLM (qwen3.5:9b, Prompt 3)", 2, 1),
    ]
    assert [each.label for each in by_key["way"].groups] == ["direkt", "indirekt über eine Beziehung"]
    assert [each.label for each in by_key["sector"].groups] == ["keine Branche (gegen SPY)", "Öl und Gas"]
    assert [each.label for each in by_key["classification"].groups] == ["belegt", "plausibel"]
    assert [each.label for each in by_key["event_type"].groups] == ["Energieversorgung"]
    # Probabilities from high to low, not by name.
    assert [each.key for each in by_key["probability"].groups] == ["high", "medium", "low"]
    assert [(each.key, each.label, each.count) for each in evaluation.months] == [("2026-11", "November 2026", 2), ("2026-12", "Dezember 2026", 1)]


def test_the_horizon_is_a_breakdown_only_when_all_are_chosen_and_an_unknown_choice_is_the_default():
    rows = [row(horizon=60), row(horizon=5), row(horizon=20)]
    pooled = evaluate(rows, "all")
    assert [each.key for each in next(b for b in pooled.breakdowns if b.key == "horizon").groups] == ["5", "20", "60"]
    assert evaluate(rows, "bogus").horizon == "20"


def test_no_rows_make_an_empty_evaluation():
    evaluation = evaluate([], "20")
    assert (evaluation.total.count, evaluation.total.band_label, evaluation.months, evaluation.judged) == (0, "–", [], 0)
    assert all(not each.groups for each in evaluation.breakdowns)


def test_the_band_is_widened_by_bonferroni_for_every_group_judged():
    few = evaluate([row()] * 5, "20")
    assert few.judged == 0 and few.z == pytest.approx(1.96, abs=0.001)
    # 40 forecasts of one model, event type, ...: the total, one group per breakdown and the month are judged - 8.
    many = evaluate([row(hit=index % 4 != 0, event_id=index) for index in range(40)], "20")
    assert many.judged == 8
    assert many.z == pytest.approx(backtest_service.threshold(8))
    assert many.level_label == "99,38 %"
    assert (many.total.low, many.total.high) == pytest.approx(wilson(30, 40, many.z))
    assert many.total.verdict == "above"
    assert forecast_evaluation_service.evaluate([row(hit=index % 4 != 0, event_id=index) for index in range(40)], "20").total.low < wilson(30, 40, 1.96)[0]
