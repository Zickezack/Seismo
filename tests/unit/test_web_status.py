"""The status page in the web layer, without a database."""
import re
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.services import status_service
from app.services.status_service import HistoryRow, KindFigures, Latency, LlmStep, QueueRow, SourceRow, Status
from app.services.storage_service import Growth, Measurement
from tests.unit.test_web_articles import tag_of, text_of
from tests.unit.test_web_events import client  # noqa: F401

NOW = datetime(2026, 11, 4, 21, 30, tzinfo=UTC)


def inner(html, testid, end="</tr>"):
    match = re.search(rf'data-testid="{testid}"[^>]*>(.*?){end}', html, re.DOTALL)
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split()) if match else None


def status(period="day", **values):
    base = dict(
        period=period, period_label="24 Stunden", start=NOW.replace(day=3), end=NOW,
        articles=SimpleNamespace(live=700, backfill=37, duplicates=33, failed=3, extracted=372, with_event=336),
        per_hour=30.7, extracted=450,
        llm_steps=[LlmStep("extract_events", 480, 12_000.0, 1_400_000, 300_000, 8, 26.7, 3778, 2.67),
                   LlmStep("assess_impacts", 200, 3_000.0, 200_000, 40_000, 62, None, None, None)],
        llm_calls=680, llm_seconds=15_000.0, llm_busy=0.17, llm_slots=1, prompt_tokens=1_600_000, answer_tokens=340_000,
        per_article_seconds=26.7, per_article_tokens=3778, cost_rappen_per_hour=360,
        kinds=[KindFigures("fetch_article", 1580, 12, 1600, 570, 1580, 30.0, 600.0, 2.0, 30.0)], attempts=1600, failed_attempts=570,
        latencies=[Latency("total", "Abruf bis Ereignis, gesamt", 410, 4080.0, 43200.0)],
        queue=[QueueRow("extract_events", 0, 3, 0, 1, 7200.0)], extraction_waiting=442,
        sources=[SourceRow(4, "Africanews", 25, 1.04, 0, 1, 20, 18, 540.0)],
        history=[HistoryRow(NOW.replace(minute=0), 25, 0, 30, 900.0, 0.25, 272, 54, 3)], history_unit="hour",
        storage=Measurement(date(2026, 11, 4), 900 * 1024**2, 3097, 2000, 200 * 1024**2, 1024**3, None),
        growth=Growth(7, 4, 70 * 1024**2, 38 * 1024**2, 207.0),
        bottlenecks=["Extraktionsbudget ausgeschöpft: 442 Artikel warten (30 von 30 pro Stunde)."],
    )
    return Status(**(base | values))


@pytest.fixture
def page(monkeypatch):
    calls = {}

    def overview(_engine, period="day"):
        calls["period"] = period
        return calls.get("status") or status()

    monkeypatch.setattr(status_service, "overview", overview)
    return calls


def test_the_page_shows_the_measured_values_and_names_the_bottlenecks(client, page):
    html = client.get("/status").text

    assert tag_of(html, "nav-status") is not None and page["period"] == "day"
    assert 'aria-current="true"' in tag_of(html, "period-day")
    assert text_of(html, "bottleneck-1") == "Extraktionsbudget ausgeschöpft: 442 Artikel warten (30 von 30 pro Stunde)."
    assert text_of(html, "articles-per-hour") == "30,7"
    assert inner(html, "failure-rate", "</td>").startswith("36 % der Job-Versuche gescheitert (570 von 1600)")
    assert text_of(html, "cost-per-article") == "27 s LLM-Zeit und 3&#39;778 Tokens je Artikel, CHF 0.0267."
    assert "27 s, 3&#39;778 Tokens" in inner(html, "llm-step-extract_events")
    assert inner(html, "llm-step-assess_impacts").endswith("62 –")
    assert inner(html, "latency-total") == "Abruf bis Ereignis, gesamt 410 68 min 12 h"
    assert inner(html, "kind-fetch_article").startswith("Volltext abrufen 1580 12 36 % 570 von 1600")
    assert inner(html, "queue-extract_events-0") == "Ereignisse extrahieren 3 0 1 2,0 h"
    assert inner(html, "source-4") == "Africanews 25 1,0 pro Stunde 0 % 4 % 80 % 30 s"
    assert text_of(html, "storage-now").startswith("Datenbank 900 MB, Rohtexte 200 MB, Backups 1,0 GB")
    links = re.findall(r'<a href="#(\w+)" data-testid="section-nav-\w+">', html)
    assert links == re.findall(r'<details class="card section" id="(\w+)"', html)


def test_without_a_cost_rate_or_bottlenecks_the_page_says_so(client, page):
    page["status"] = status("week", bottlenecks=[], cost_rappen_per_hour=0, storage=None, growth=None, queue=[], extracted=0,
                            per_article_seconds=None, per_article_tokens=None)
    html = client.get("/status?period=week").text

    assert page["period"] == "week"
    assert text_of(html, "no-bottlenecks") == "Kein Engpass nach den Regeln."
    assert text_of(html, "cost-per-article") == "Keine LLM-Aufrufe für Artikel erfasst in diesem Zeitraum."
    assert text_of(html, "empty-queue") == "Die Warteschlange ist leer."
    assert text_of(html, "storage-now") == "Noch nicht gemessen: der Worker misst einmal am Tag."
