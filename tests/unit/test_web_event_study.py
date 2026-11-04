"""The Event-Study page in the web layer, without a database: the service's computation is replaced."""
import re
from datetime import date

import pytest

from app.services import event_study_service
from app.services.event_study_service import WINDOWS, Benchmark, Exclusion, Figure, Measure, Overview, OverviewRow, Period
from tests.unit.test_web_articles import tag_of, text_of
from tests.unit.test_web_events import client  # noqa: F401


def overview(benchmark=Benchmark.MARKET, measure=Measure.MODEL, training=None):
    periods = [Period.TRAINING, Period.TEST] if training else [Period.UNDIVIDED]
    measured = Figure([0.05, -0.01, 0.02])
    excluded = Figure()
    excluded.excluded[Exclusion.GAP] += 1
    excluded.excluded[Exclusion.OPEN] += 2
    rows = [
        OverviewRow(None, "Alle Typen", WINDOWS[1], {period: measured for period in periods}),
        OverviewRow(None, "Alle Typen", WINDOWS[3], {period: excluded for period in periods}),
        OverviewRow("natural_disaster", "Naturkatastrophe", WINDOWS[1], {period: measured for period in periods}),
    ]
    return Overview(benchmark, measure, periods, rows, 2, 3, training, None, [])


@pytest.fixture
def study(monkeypatch):
    calls = {}

    def computed(_engine, benchmark, measure):
        calls["choice"] = (benchmark, measure)
        return calls.get("overview") or overview(benchmark, measure)

    monkeypatch.setattr(event_study_service, "overview", computed)
    return calls


def test_the_page_shows_count_mean_median_and_share_per_type_window_and_period(client, study):
    html = client.get("/event-study").text

    assert study["choice"] == (Benchmark.MARKET, Measure.MODEL)
    assert text_of(html, "study-counts").startswith("2 Ereignisse mit 3 Wertpapieren.")
    assert text_of(html, "study-periods") == "nicht aufgeteilt"
    figure = re.search(r'data-testid="figure-all-day-undivided">(.*?)</td>', html, re.DOTALL).group(1)
    assert all(part in figure for part in ("n 3", "Ø +2,00 %", "Median +2,00 %", "67 % positiv"))
    assert text_of(html, "excluded-all-d5-undivided") == "ohne 3: offen 2, Lücke 1"
    assert '<a href="/events?type=natural_disaster">Naturkatastrophe</a>' in html
    assert tag_of(html, "nav-event-study") is not None


def test_the_choice_of_benchmark_and_measure_comes_from_the_url(client, study):
    html = client.get("/event-study?benchmark=sector&measure=excess").text

    assert study["choice"] == (Benchmark.SECTOR, Measure.EXCESS)
    assert '<option value="sector" selected>' in html and '<option value="excess" selected>' in html


def test_the_periods_are_columns_of_their_own(client, study):
    study["overview"] = overview(training=date(2025, 12, 31))

    html = client.get("/event-study").text

    assert text_of(html, "study-periods") == "Training bis 31.12.2025, danach Test"
    assert text_of(html, "period-training") == "Training" and text_of(html, "period-test") == "Test"
    assert tag_of(html, "figure-all-day-training") is not None
