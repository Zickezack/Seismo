"""Job page and Plattform-Einstellungen in the web layer, without a database: the services are replaced.

Logins and the 401 for every route without a session: tests/unit/test_web_auth.py.
"""
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.db.models import JobStatus
from app.services import extraction_service, job_service, platform_settings_service, seed_service, source_service
from app.services.job_service import BulkResult, JobListItem, JobOverview
from app.services.platform_settings_service import PlatformSettings
from app.services.seed_service import SeedStatus
from app.web.main import create_app

SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d")
AUTH = AuthSettings(admin_username="admin", admin_password="correct horse battery", session_secret="s" * 32)
NOW = datetime(2026, 10, 22, 21, 40, 5, tzinfo=UTC)
JSON = {"Accept": "application/json"}


def job(job_id, status, **fields):
    values = dict(
        id=job_id, kind="testjob", status=status, attempts=1, max_attempts=5, priority=0, created_at=NOW, updated_at=NOW,
        run_after=NOW, lease_expires_at=None, lease_owner=None, last_error=None,
    )
    return JobListItem(**values | fields)


OVERVIEW = JobOverview(
    status=None,
    counts={JobStatus.QUEUED: 0, JobStatus.RUNNING: 1, JobStatus.SUCCEEDED: 0, JobStatus.FAILED: 1},
    jobs=[
        job(8, JobStatus.RUNNING, lease_expires_at=NOW, lease_owner="a1b2c3:7"),
        job(7, JobStatus.FAILED, attempts=5, last_error="TimeoutError: <Ollama>"),
    ],
    llm_slots_in_use=1,
    llm_max_parallel_calls=2,
)


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def services(monkeypatch):
    """Replace the services; `calls` records what the routes passed on."""
    calls = {}

    def overview(_engine, status):
        calls["overview"] = status
        return replace(OVERVIEW, status=job_service.parse_status(status))

    def requeue_failed(_engine, job_ids):
        calls["requeue"] = job_ids
        return calls.get("requeue_result", BulkResult(processed=len(job_ids), blocked=[]))

    def save(_engine, form):
        calls["save"] = form
        for name, raw in form.items():
            platform_settings_service.FORM_FIELDS[name].parse(raw)

    monkeypatch.setattr(job_service, "overview", overview)
    monkeypatch.setattr(job_service, "requeue_failed", requeue_failed)
    monkeypatch.setattr(job_service, "enqueue_test_job", lambda _engine: 42)
    monkeypatch.setattr(platform_settings_service, "current", lambda _engine: PlatformSettings(llm_max_parallel_calls=3))
    monkeypatch.setattr(platform_settings_service, "save", save)
    monkeypatch.setattr(seed_service, "sources_status", lambda _engine: SeedStatus(total=17, imported=0))
    # The settings page counts the sources without a region.
    monkeypatch.setattr(source_service, "list_sources", lambda _engine, region=None, sort=None: [])
    return calls


def text_of(html, testid):
    """Text of the element with this data-testid - never a search through the whole page."""
    match = re.search(rf'data-testid="{testid}"[^>]*>([^<]*)<', html)
    return match.group(1).strip() if match else None


def tag_of(html, testid):
    """The opening tag of the element with this data-testid, to check its attributes."""
    match = re.search(rf'<[^>]*data-testid="{testid}"[^>]*>', html)
    return match.group(0) if match else None


def flash_after(client, response):
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    return text_of(page, "flash"), tag_of(page, "flash")


def test_job_list_shows_counts_jobs_and_llm_slots(client, services):
    html = client.get("/jobs").text

    assert services["overview"] is None
    assert text_of(html, "count-failed") == "1"
    assert text_of(html, "count-queued") == "0"
    assert text_of(html, "status-7") == "Fehlgeschlagen"
    assert text_of(html, "status-8") == "Läuft"
    assert text_of(html, "llm-slots") == "LLM-Aufrufe: 1 von 2 belegt ·"
    # UTC as stored; static/app.js shows it in the viewer's local time.
    assert '<td data-testid="created-8"><time datetime="2026-10-22T21:40:05+00:00" data-local-time>2026-10-22 21:40:05 UTC</time></td>' in html
    assert text_of(html, "error-7") == "TimeoutError: &lt;Ollama&gt;"


def test_only_failed_jobs_can_be_selected_and_the_bulk_button_starts_disabled(client, services):
    html = client.get("/jobs").text

    assert " disabled" not in tag_of(html, "select-7")
    assert " disabled" in tag_of(html, "select-8")
    assert " disabled" in tag_of(html, "requeue")


def test_status_filter_is_passed_on_and_marked(client, services):
    html = client.get("/jobs?status=failed").text

    assert services["overview"] == "failed"
    assert 'aria-current="true"' in tag_of(html, "filter-failed")
    assert "aria-current" not in tag_of(html, "filter-all")
    # The bulk action returns to the same filter.
    assert '<input type="hidden" name="status" value="failed">' in html


def test_requeue_flashes_the_result_once_and_returns_to_the_filtered_list(client, services):
    response = client.post("/jobs/requeue", data={"job_id": ["7", "9"], "status": "failed"})

    assert services["requeue"] == [7, 9]
    assert response.headers["location"] == "/jobs?status=failed"
    text, tag = flash_after(client, response)
    assert text == "2 Jobs erneut eingereiht."
    assert "message-success" in tag
    assert text_of(client.get("/jobs").text, "flash") is None


def test_blocked_requeue_names_the_blockers(client, services):
    services["requeue_result"] = BulkResult(processed=0, blocked=[f"Job #{n} (Läuft)" for n in range(1, 13)])

    text, tag = flash_after(client, client.post("/jobs/requeue", data={"job_id": ["1"]}))

    shown = ", ".join(f"Job #{n} (Läuft)" for n in range(1, 11))
    assert text == f"Nichts eingereiht, blockiert: {shown} und 2 weitere."
    assert "message-error" in tag


def test_requeue_without_selection(client, services):
    text, _tag = flash_after(client, client.post("/jobs/requeue", data={}))

    assert services["requeue"] == []
    assert text == "Keine Jobs ausgewählt."


def test_requeue_never_redirects_to_an_unknown_filter(client, services):
    response = client.post("/jobs/requeue", data={"job_id": ["7"], "status": "//evil.example"})

    assert response.headers["location"] == "/jobs"


def test_test_job_is_enqueued_with_a_message(client, services):
    text, _tag = flash_after(client, client.post("/jobs/test"))

    assert text == "Testjob #42 eingereiht."


def input_of(html, name):
    return re.search(rf'<input id="{name}"[^>]*>', html, re.DOTALL).group(0)


def test_settings_page_shows_every_setting_in_its_section(client, services):
    html = client.get("/settings/platform").text

    assert 'value="3"' in input_of(html, "llm_max_parallel_calls")
    assert 'min="1"' in input_of(html, "llm_max_parallel_calls") and 'max="32"' in input_of(html, "llm_max_parallel_calls")
    assert 'value="7"' in input_of(html, "succeeded_jobs_retention_days")
    assert 'value="30"' in input_of(html, "failed_jobs_retention_days")
    assert 'value="7"' in input_of(html, "article_max_age_days")
    assert 'value="10"' in input_of(html, "domain_min_interval_seconds")
    assert 'max="600"' in input_of(html, "domain_min_interval_seconds")
    assert 'min="1"' in input_of(html, "article_max_age_days") and 'max="365"' in input_of(html, "article_max_age_days")
    # The domains without full text, one per line, starting with the publishers that forbid text and data mining.
    domains = re.search(r'<textarea id="tdm_blocked_domains"[^>]*>([^<]*)</textarea>', html).group(1).split()
    assert "bbc.co.uk" in domains and "theguardian.com" in domains
    sections = re.findall(r'data-testid="form-(\w+)"(.*?)</form>', html, re.DOTALL)
    assert [(name, re.findall(r'name="(\w+)"', form)) for name, form in sections] == [
        ("verarbeitung", ["llm_max_parallel_calls"]),
        ("abruf", ["article_max_age_days", "domain_min_interval_seconds", "tdm_blocked_domains"]),
        ("extraktion", ["extraction_languages", "extraction_min_text_chars", "extraction_max_age_days", "extractions_per_hour", "source_quality_domains"]),
        ("aufbewahrung", ["succeeded_jobs_retention_days", "failed_jobs_retention_days"]),
        ("backups", ["backup_interval_hours", "backups_to_keep"]),
    ]


def test_settings_page_links_every_section_from_its_quick_navigation(client, services):
    html = client.get("/settings/platform").text

    links = re.findall(r'<a href="#(\w+)" data-testid="section-nav-\w+">', html)
    assert links == re.findall(r'<details class="card section" id="(\w+)"', html)
    assert links == ["verarbeitung", "abruf", "extraktion", "aufbewahrung", "backups", "starterdaten"]


def test_settings_save_in_place_answers_json(client, services):
    form = {"succeeded_jobs_retention_days": "3", "failed_jobs_retention_days": "60"}

    response = client.post("/settings/platform", data=form, headers=JSON)

    assert response.status_code == 200
    assert response.json() == {"status": "Gespeichert"}
    assert services["save"] == form


def test_invalid_setting_answers_json_with_the_message(client, services):
    response = client.post("/settings/platform", data={"llm_max_parallel_calls": "0"}, headers=JSON)

    # 200: browsers log every 4xx answer as a console error.
    assert response.status_code == 200
    assert response.json() == {"error": "Parallele LLM-Aufrufe (höchstens): bitte eine ganze Zahl von 1 bis 32 eingeben."}


@pytest.mark.parametrize(
    ("value", "message", "kind"),
    [("4", "Gespeichert", "success"), ("abc", "Parallele LLM-Aufrufe (höchstens): bitte eine ganze Zahl von 1 bis 32 eingeben.", "error")],
)
def test_settings_save_without_javascript_shows_the_page_again(client, services, value, message, kind):
    text, tag = flash_after(client, client.post("/settings/platform", data={"llm_max_parallel_calls": value}))

    assert text == message
    assert f"message-{kind}" in tag


def test_navigation_marks_the_current_page(client, services):
    html = client.get("/settings/platform").text

    assert 'aria-current="page"' in tag_of(html, "nav-settings")
    assert "aria-current" not in tag_of(html, "nav-jobs")
    assert "aria-current" not in tag_of(html, "nav-dashboard")


def test_login_page_has_no_navigation(services):
    with TestClient(create_app(SETTINGS, AUTH)) as anonymous:
        assert tag_of(anonymous.get("/login").text, "nav-jobs") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "Kein Ollama konfiguriert (OLLAMA_BASE_URL): der Worker extrahiert nichts."),
        (extraction_service.LlmStatus("http://ollama:11434", "qwen3.5:9b", 8192, True), "Ollama http://ollama:11434, Modell qwen3.5:9b, Kontext 8192 Tokens: verfügbar"),
        (extraction_service.LlmStatus("http://ollama:11434", "qwen3.5:9b", 8192, False), "Ollama http://ollama:11434, Modell qwen3.5:9b, Kontext 8192 Tokens: nicht verfügbar - nicht erreichbar oder das Modell nicht geladen (ollama pull)"),
    ],
    ids=["none configured", "served", "not served"],
)
def test_the_extraction_section_says_which_ollama_and_whether_it_serves_the_model(client, services, monkeypatch, status, expected):
    monkeypatch.setattr(extraction_service, "llm_status", lambda *_llm: status)

    html = client.get("/settings/platform").text

    line = re.sub(r"<[^>]+>", "", re.search(r'data-testid="llm-status">(.*?)</p>', html).group(1))
    assert line.startswith(expected) and line.endswith("Festgelegt in der Umgebung des Servers, nicht hier.")
    assert 'data-testid="source_quality_domains"' in html and "ecb.europa.eu hoch" in html
