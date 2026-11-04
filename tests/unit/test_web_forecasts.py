"""The Prognosen page in the web layer, without a database."""
import re
from datetime import date

import pytest

from app.services import forecast_service
from app.services.forecast_service import HitRate, Overview
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

    monkeypatch.setattr(forecast_service, "overview", overview)
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
