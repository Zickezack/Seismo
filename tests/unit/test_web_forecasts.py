"""The Prognosen page in the web layer, without a database."""
import re
from datetime import date

import pytest

from app.services import forecast_evaluation_service, forecast_service
from app.services.forecast_evaluation_service import Breakdown, Evaluation, Group, evaluate
from app.services.forecast_service import HitRate, Overview
from tests.unit.test_forecast_evaluation_service import row
from tests.unit.test_forecast_service import view
from tests.unit.test_web_articles import tag_of, text_of
from tests.unit.test_web_events import client  # noqa: F401


@pytest.fixture
def forecasts(monkeypatch):
    calls = {}

    def overview(_engine, status=""):
        calls["status"] = status
        measured = view(id=2, outcome_status="measured", asset_return=-0.05, benchmark_return=0.01, excess_return=-0.06, hit=True)
        return calls.get("overview") or Overview([view(), measured], 1, 1, 0, [HitRate(20, "high", 1, 1, 0.06)], status)

    def evaluation(_engine, horizon="20"):
        calls["horizon"] = horizon
        return calls.get("evaluation") or evaluate([], horizon)

    monkeypatch.setattr(forecast_service, "overview", overview)
    monkeypatch.setattr(forecast_evaluation_service, "evaluation", evaluation)
    return calls


def test_the_page_lists_forecasts_with_their_outcome_and_the_hit_rates(client, forecasts):
    html = client.get("/forecasts").text

    assert text_of(html, "forecast-counts") == "1 offen, 1 gemessen, 0 nicht messbar."
    assert text_of(html, "forecast-outcome-1") == "offen bis 02.12.2026"
    assert text_of(html, "forecast-outcome-2") == "-6,00 % gegen XLE: Treffer"
    assert text_of(html, "forecast-says-1") == "negativ gegenüber dem Benchmark"
    rate = re.search(r'data-testid="rate-20-high">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "1 (100 %)" in rate and "+6,00 %" in rate
    assert tag_of(html, "nav-forecasts") is not None


def test_the_filter_comes_from_the_url(client, forecasts):
    forecasts["overview"] = Overview([], 0, 0, 0, [], "open")
    html = client.get("/forecasts?status=open").text
    assert forecasts["status"] == "open"
    assert '<option value="open" selected>' in html
    assert text_of(html, "no-forecasts") == "Keine Prognosen in dieser Auswahl."


def test_the_breakdown_shows_each_group_with_its_count_band_and_verdict(client, forecasts):
    forecasts["evaluation"] = evaluate([row(hit=index % 4 != 0, event_id=index % 10) for index in range(40)], "20")
    html = client.get("/forecasts").text

    assert forecasts["horizon"] == "20"
    assert [tag_of(html, f"section-nav-{key}") is not None for key in ("rates", "breakdown", "trend", "list")] == [True] * 4
    total = re.search(r'data-testid="evaluation-total-all">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "40" in total and "aus 10 Ereignissen" in total and "30 (75 %)" in total
    assert 'data-verdict="above"' in total and "besser als Zufall" in total
    made_by = re.search(r'data-testid="breakdown-made_by-llm:qwen3.5:9b:3">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "LLM (qwen3.5:9b, Prompt 3)" in made_by
    assert tag_of(html, "breakdown-horizon") is None
    assert "für alle 8 beurteilten Gruppen" in text_of(html, "evaluation-rule")
    assert tag_of(html, "trend-2026-11") is not None


def test_a_group_with_few_is_not_judged_and_the_horizon_comes_from_the_url(client, forecasts):
    forecasts["evaluation"] = Evaluation("all", Group("all", "Alle", 3, 1, 3, 0.02, 0.4, 1.0), [Breakdown("horizon", "Horizont", [])], [], 0, 1.96)
    html = client.get("/forecasts?horizon=all&status=open").text

    assert (forecasts["horizon"], forecasts["status"]) == ("all", "open")
    assert '<option value="all" selected>' in html
    assert 'data-verdict="few"' in html and ">zu wenige</span>" in html
    # Each filter keeps the other's choice.
    assert '<input type="hidden" name="horizon" value="all">' in html
    assert '<input type="hidden" name="status" value="open">' in html


def test_without_measured_forecasts_the_breakdown_says_so(client, forecasts):
    html = client.get("/forecasts?horizon=60").text
    assert text_of(html, "no-evaluation") == "Noch keine gemessenen Prognosen über 60 Handelstage."
    assert text_of(html, "no-trend") == "Noch keine gemessenen Prognosen über 60 Handelstage."
