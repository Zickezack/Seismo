"""Datenbestand page in the web layer, without a database: the services are replaced."""
import re
from datetime import date
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.services import reprocessing_service, storage_service
from app.services.bulk import BulkResult
from app.services.reprocessing_service import Group, InvalidReprocessing, Overview, Preview, ReprocessingView
from app.services.storage_service import Measurement, SourceRawTexts, Storage, TableSize
from app.web.main import create_app
from tests.unit.test_web_articles import AUTH, NOW, SETTINGS, tag_of, text_of


def choice_of(html, group):
    """The radio button of a Stand; its key holds characters a pattern would read."""
    return re.search(rf'<input[^>]*data-testid="choose-{re.escape(group.key)}"[^>]*>', html).group(0)

MODEL = SETTINGS.ollama_model
OLD = Group("extraction", "1", MODEL, 30, False)
NOW_GROUP = Group("extraction", "2", MODEL, 500, True)
STORAGE = Storage(
    database_bytes=337_641_472, tables=[TableSize("raw_text", 297_795_584, 0.88), TableSize("article", 16_777_216, 0.05)],
    article_count=6500, raw_text_count=4826, raw_text_bytes=268_435_456, backup_count=2, backup_bytes=104_857_600,
    backup_free_bytes=None, by_source=[SourceRawTexts(3, "GDELT – Energie <1>", 2000, 157_286_400), SourceRawTexts(None, "Alt", 4, 4096)],
)


def reprocessing(reprocessing_id, **fields):
    values = dict(
        id=reprocessing_id, step="extraction", schema_version="1", model=MODEL, source_name=None, period_start=None,
        period_end=None, status="running", total=30, made=12, open_jobs=3, failed_jobs=1, created_at=NOW, finished_at=None,
    )
    return ReprocessingView(**values | fields)


def overview(**fields):
    values = dict(groups={"extraction": [NOW_GROUP, OLD], "recognition": []}, reprocessings=[], sources=[], budget=30, model=MODEL)
    return Overview(**values | fields)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def page(monkeypatch):
    calls = {"overview": overview(), "measurements": []}

    def fake_overview(_engine, model, form=None):
        calls.setdefault("forms", []).append((model, form))
        if isinstance(calls.get("invalid"), Exception) and form:
            raise calls["invalid"]
        return calls["overview"]

    monkeypatch.setattr(reprocessing_service, "overview", fake_overview)
    monkeypatch.setattr(storage_service, "current", lambda _engine, backup_dir: STORAGE)
    monkeypatch.setattr(storage_service, "history", lambda _engine: calls["measurements"])
    return calls


def test_the_page_shows_the_storage_now_its_tables_and_the_raw_texts_per_source(client, page):
    html = client.get("/data").text

    assert text_of(html, "storage-database") == "322 MB"
    assert re.search(r'data-testid="storage-raw-texts">256 MB <span class="hint">4826 von 6500 Artikeln', html)
    assert text_of(html, "storage-free") == "–"
    assert re.findall(r'data-testid="table-(\w+)"', html) == ["raw_text", "article"]
    row = re.search(r'data-testid="raw-texts-1">(.*?)</tr>', html, re.DOTALL).group(1)
    assert '<a href="/sources/3">GDELT – Energie &lt;1&gt;</a>' in row and "150 MB" in row and "77 kB" in row
    assert "Alt <span class=\"hint\">gelöscht</span>" in html
    # Without measurements yet, the page says when they come.
    assert text_of(html, "no-measurements") == "Noch keine Messung. Der Worker misst einmal am Tag (UTC)."
    assert 'aria-current="page"' in tag_of(html, "nav-data")


def test_every_section_is_linked_from_the_quick_navigation(client, page):
    html = client.get("/data").text

    assert re.findall(r'<a href="#([\w-]+)" data-testid="section-nav-[\w-]+">', html) == re.findall(r'<details class="card section" id="([\w-]+)"', html)


def test_the_history_shows_each_day_and_the_growth_per_day(client, page):
    page["measurements"] = [
        Measurement(date(2026, 10, 31), 3_145_728, 120, 100, 2_097_152, 0, 1_073_741_824),
        Measurement(date(2026, 10, 30), 2_097_152, 100, 80, 1_048_576, 0, None),
    ]

    html = client.get("/data").text

    assert re.findall(r'data-testid="measurement-([\d-]+)"', html) == ["2026-10-31", "2026-10-30"]
    growth = re.search(r'data-testid="growth-7">(.*?)</tr>', html, re.DOTALL).group(1)
    assert "gemessen über 1 Tag" in growth and "1,0 MB" in growth and "20" in growth


def test_the_groups_offer_only_the_outdated_and_the_start_needs_a_choice(client, page):
    html = client.get("/data").text

    assert "disabled" in choice_of(html, NOW_GROUP) and "disabled" not in choice_of(html, OLD)
    start = tag_of(html, "start-reprocessing")
    # Enabled once a Stand is chosen (static/app.js); the dialog names it, then it posts the form's fields.
    assert 'formaction="/data/reprocessings"' in start and 'data-confirm="Stand|Stände"' in start and "data-bulk-action" in start
    assert f'data-name="Ereignisextraktion, Schema 1, {MODEL}"' in choice_of(html, OLD)


def test_while_one_runs_no_other_starts(client, page):
    page["overview"] = overview(reprocessings=[reprocessing(4)])

    html = client.get("/data").text

    start = tag_of(html, "start-reprocessing")
    assert "disabled" in start and "data-bulk-action" not in start
    assert re.search(r'data-testid="reprocessing-progress-4">12 von 30 neu<span class="hint">3 in der Warteschlange', html)
    assert "1 fehlgeschlagen (Jobs)" in html
    assert "disabled" not in tag_of(html, "select-reprocessing-4")


def test_a_choice_shows_what_it_would_take_and_keeps_its_fields(client, page):
    page["overview"] = overview(preview=Preview(OLD, "EZB", date(2026, 10, 1), None, 28, 2))

    html = client.get("/data", params={"group": OLD.key, "source_id": "3", "period_start": "2026-10-01"}).text

    assert page["forms"][-1] == (MODEL, {"group": OLD.key, "source_id": "3", "period_start": "2026-10-01", "period_end": ""})
    assert text_of(html, "reprocess-preview") == (
        f"Ereignisextraktion, Schema 1, {MODEL}, EZB, 01.10.2026 bis …: 28 Läufe neu zu verarbeiten. 2 weitere ohne Text, nicht möglich."
    )
    assert "checked" in choice_of(html, OLD) and 'value="2026-10-01"' in tag_of(html, "reprocess-period-start")


def test_an_invalid_choice_shows_its_error_and_the_page_still(client, page):
    page["invalid"] = InvalidReprocessing("Von: bitte ein Datum wählen.")

    html = client.get("/data", params={"group": OLD.key, "period_start": "x"}).text

    assert text_of(html, "reprocess-error") == "Von: bitte ein Datum wählen."
    assert page["forms"][-1] == (MODEL, None)


def test_starting_reports_the_runs_or_returns_to_the_choice_with_its_error(client, page, monkeypatch):
    calls = []

    def start(_engine, form, model):
        calls.append((form, model))
        if form.get("period_start") == "x":
            raise InvalidReprocessing("Von: bitte ein Datum wählen.")
        return reprocessing(5, total=30)

    monkeypatch.setattr(reprocessing_service, "start", start)

    response = client.post("/data/reprocessings", data={"group": OLD.key})
    assert calls[-1] == ({"group": OLD.key}, MODEL) and response.headers["location"] == "/data#neuverarbeitungen"
    assert text_of(client.get("/data").text, "flash") == "Neuverarbeitung gestartet: 30 Läufe der Ereignisextraktion."

    response = client.post("/data/reprocessings", data={"group": OLD.key, "period_start": "x"})
    choice = urlencode({"group": OLD.key, "source_id": "", "period_start": "x", "period_end": ""})
    assert response.headers["location"] == f"/data?{choice}#neu-verarbeiten"
    assert text_of(client.get("/data").text, "flash") == "Neuverarbeitung nicht gestartet: Von: bitte ein Datum wählen."


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (BulkResult(processed=1, blocked=[]), "1 Neuverarbeitung abgebrochen."),
        (BulkResult(processed=0, blocked=["Neuverarbeitung #4 (Abgeschlossen)"]), "Nichts abgebrochen, blockiert: Neuverarbeitung #4 (Abgeschlossen)."),
        (BulkResult(processed=0, blocked=[]), "Keine Neuverarbeitungen ausgewählt."),
    ],
)
def test_cancelling_passes_the_selection_and_reports_the_result(client, page, monkeypatch, result, message):
    calls = []
    monkeypatch.setattr(reprocessing_service, "cancel", lambda _engine, ids: calls.append(ids) or result)

    response = client.post("/data/reprocessings/cancel", data={"reprocessing_id": ["4"]})

    assert calls == [[4]] and response.headers["location"] == "/data#neuverarbeitungen"
    assert text_of(client.get("/data").text, "flash") == message
