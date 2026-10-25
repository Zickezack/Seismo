"""Quellen pages and the starter catalogue import in the web layer, without a database: the services are replaced.

Logins and the 401 for every route without a session: tests/unit/test_web_auth.py.
"""
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.db.models import BackfillStatus
from app.services import (
    article_service,
    backfill_service,
    ingestion_service,
    platform_settings_service,
    seed_service,
    source_service,
)
from app.services.backfill_service import Backfill, InvalidBackfill
from app.services.bulk import BulkResult
from app.services.platform_settings_service import PlatformSettings
from app.services.seed_service import SeedResult, SeedStatus
from app.services.source_service import InvalidSource, Source
from app.web.main import create_app

SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d")
AUTH = AuthSettings(admin_username="admin", admin_password="correct horse battery", session_secret="s" * 32)
NOW = datetime(2026, 10, 23, 9, 30, 0, tzinfo=UTC)
JSON = {"Accept": "application/json"}


def source(source_id, name, **fields):
    values = dict(
        id=source_id, name=name, adapter_type="rss", url=f"https://example.org/{source_id}.xml", query=None, is_active=True, extract_events=True, quality=None,
        fetch_interval_minutes=60, language="en", region="europe", terms_note=None, raw_text_retention_days=30,
        min_request_interval_seconds=None, created_at=NOW, updated_at=NOW, last_fetched_at=None, last_error=None,
        last_error_at=None, consecutive_failures=0,
    )
    return Source(**values | fields)


SOURCES = [
    source(2, "Apfel <Feed>", last_fetched_at=NOW),
    source(1, "Zentralbank", is_active=False, language=None, raw_text_retention_days=1, terms_note="Nur mit Quellenangabe"),
    source(
        4, "Ausfall", last_fetched_at=NOW, last_error="HTTP 503 (Retry-After 120 s)", last_error_at=NOW,
        consecutive_failures=3, min_request_interval_seconds=60,
    ),
    source(5, "GDELT <Zentralbanken>", adapter_type="gdelt", url="https://api.gdeltproject.org/api/v2/doc/doc", query='"central bank" <sourcelang:german>'),
]
FORM = {
    "name": "Neu",
    "adapter_type": "rss",
    "url": "https://example.org/neu.xml",
    "is_active": "on",
    "fetch_interval_minutes": "15",
    "language": "de",
    "terms_note": "",
    "raw_text_retention_days": "7",
    "region": "asia",
}


@pytest.fixture
def client():
    with TestClient(create_app(SETTINGS, AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


@pytest.fixture
def services(monkeypatch):
    """Replace the services; `calls` records what the routes passed on."""
    calls = {"sources": SOURCES}
    found = {each.id: each for each in SOURCES}

    def create(_engine, form):
        calls["create"] = form
        values = source_service.parse(form)
        return source(3, values.name)

    def update(_engine, source_id, form):
        calls["update"] = (source_id, form)
        source_service.parse(form)
        return found.get(source_id)

    def bulk(name):
        def action(_engine, source_ids, *flag):
            calls[name] = (source_ids, *flag)
            return calls.get(f"{name}_result", BulkResult(processed=len(source_ids), blocked=[]))

        return action

    def import_sources(_engine):
        calls["import"] = True
        return calls.get("import_result", SeedResult(imported=17, skipped=0, name_taken=[]))

    def list_sources(_engine, region=None, sort=None):
        calls["list_sources"] = (region, sort)
        return calls["sources"] if region in (None, "") else calls.get("sources_in_region", [])

    monkeypatch.setattr(source_service, "list_sources", list_sources)
    monkeypatch.setattr(source_service, "get", lambda _engine, source_id: found.get(source_id))
    monkeypatch.setattr(source_service, "create", create)
    monkeypatch.setattr(source_service, "update", update)
    monkeypatch.setattr(source_service, "delete", bulk("delete"))
    monkeypatch.setattr(source_service, "set_active", bulk("set_active"))
    monkeypatch.setattr(ingestion_service, "fetch_now", bulk("fetch_now"))
    monkeypatch.setattr(article_service, "count_by_source", lambda _engine: {2: 12})
    monkeypatch.setattr(article_service, "blocked_by_source", lambda _engine: calls.get("blocked_counts", {}))
    monkeypatch.setattr(ingestion_service, "refetch_blocked", bulk("refetch_blocked"))
    monkeypatch.setattr(seed_service, "import_sources", import_sources)
    monkeypatch.setattr(seed_service, "take_catalogue_regions", lambda _engine: calls.get("regions_given", 0))
    monkeypatch.setattr(seed_service, "sources_status", lambda _engine: calls.get("seed_status", SeedStatus(17, 0)))
    monkeypatch.setattr(platform_settings_service, "current", lambda _engine: PlatformSettings())

    def start_backfill(_engine, source_id, form):
        calls["start_backfill"] = (source_id, form)
        if "start_backfill_error" in calls:
            raise InvalidBackfill(calls["start_backfill_error"])
        return backfill(9, source_id)

    monkeypatch.setattr(backfill_service, "list_for_source", lambda _engine, source_id: calls.get("backfills", []))
    monkeypatch.setattr(backfill_service, "start", start_backfill)
    def cancel_backfills(_engine, source_id, backfill_ids):
        calls["cancel_backfills"] = (source_id, backfill_ids)
        return calls["cancel_backfills_result"]

    monkeypatch.setattr(backfill_service, "cancel", cancel_backfills)
    return calls


def backfill(backfill_id, source_id, **fields):
    values = dict(
        id=backfill_id, source_id=source_id, period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 10, 1, tzinfo=UTC), cursor=datetime(2026, 9, 20, 6, 0, tzinfo=UTC),
        status=BackfillStatus.RUNNING, articles_found=500, articles_new=480, last_error=None, created_at=NOW,
        finished_at=None, full_texts_stored=120, full_texts_pending=360,
    )
    return Backfill(**values | fields)


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


def test_list_shows_the_sources_in_the_order_the_service_returns(client, services):
    html = client.get("/sources").text

    assert re.findall(r'data-testid="source-(\d+)"', html) == ["2", "1", "4", "5"]
    assert text_of(html, "name-2") == "Apfel &lt;Feed&gt;"
    assert text_of(html, "url-2") == "https://example.org/2.xml"
    assert text_of(html, "status-2") == "Aktiv"
    assert text_of(html, "status-1") == "Inaktiv"
    assert text_of(html, "language-2") == "Englisch"
    assert text_of(html, "language-1") == "Mehrsprachig oder unbekannt"
    assert text_of(html, "interval-2") == "alle 60 Min."
    assert text_of(html, "retention-2") == "30 Tage"
    assert text_of(html, "retention-1") == "1 Tag"
    assert text_of(html, "articles-2") == "12"
    assert 'href="/articles?source=2"' in tag_of(html, "articles-2")
    assert text_of(html, "articles-1") == "0"
    assert text_of(html, "fetched-1") == "–"
    # A GDELT source shows its Suchanfrage under the API address, escaped like every other text.
    assert text_of(html, "query-5") == "&#34;central bank&#34; &lt;sourcelang:german&gt;"
    assert tag_of(html, "query-2") is None


def test_list_shows_the_fetch_status_and_the_last_error_while_failing(client, services):
    html = client.get("/sources").text

    assert re.search(r'data-testid="fetch-status-2"><span class="status status-ok">OK<', html)
    assert re.search(r'data-testid="fetch-status-1"><span class="status status-new">Noch nicht abgerufen<', html)
    assert re.search(r'data-testid="fetch-status-4"><span class="status status-failing">3 Fehler in Folge<', html)
    assert re.search(
        r'data-testid="last-error-4"><time datetime="2026-10-23T09:30:00\+00:00" data-local-time>2026-10-23 09:30:00 UTC</time>: '
        r"HTTP 503 \(Retry-After 120 s\)</span>",
        html,
    )
    assert tag_of(html, "last-error-2") is None


def test_bulk_buttons_start_disabled_and_delete_asks_first(client, services):
    html = client.get("/sources").text

    for action in ("fetch", "activate", "deactivate", "delete"):
        assert " disabled" in tag_of(html, action)
    assert 'data-confirm="Quelle|Quellen"' in tag_of(html, "delete")
    # The dialog names what it deletes, from the row checkboxes - escaped like every other text.
    assert 'data-name="Apfel &lt;Feed&gt;"' in tag_of(html, "select-2")
    assert tag_of(html, "confirm-dialog") is not None


def test_empty_list_points_to_the_starter_catalogue(client, services):
    services["sources"] = []

    html = client.get("/sources").text

    assert 'href="/settings/platform#starterdaten"' in html
    assert tag_of(html, "no-sources") is not None
    assert tag_of(html, "select-all") is None


def test_new_source_form_starts_with_the_defaults(client, services):
    html = client.get("/sources/new").text

    assert text_of(html, "heading") == "Neue Quelle"
    assert 'action="/sources"' in tag_of(html, "source-form")
    assert "data-inline-save" not in tag_of(html, "source-form")
    assert " checked" in tag_of(html, "is_active")
    assert " checked" in tag_of(html, "extract_events")
    assert '<option value="" selected>Unbekannt</option>' in html and '<option value="high">Hoch</option>' in html
    assert 'value="60"' in re.search(r'<input id="fetch_interval_minutes"[^>]*>', html, re.DOTALL).group(0)
    assert 'value="30"' in re.search(r'<input id="raw_text_retention_days"[^>]*>', html, re.DOTALL).group(0)


def test_create_goes_back_to_the_list_with_a_message(client, services):
    response = client.post("/sources", data=FORM)

    assert response.headers["location"] == "/sources"
    assert services["create"] == FORM
    text, tag = flash_after(client, response)
    assert text == "Quelle „Neu“ angelegt."
    assert "message-success" in tag


def test_invalid_create_shows_the_form_again_with_the_entries(client, services):
    response = client.post("/sources", data=FORM | {"url": "example.org", "is_active": ""} | {"name": "Mein <Feed>"})

    assert response.status_code == 200
    assert text_of(response.text, "form-error") == "URL: bitte eine vollständige Adresse mit http:// oder https:// angeben."
    assert 'value="Mein &lt;Feed&gt;"' in response.text
    assert 'value="example.org"' in response.text
    assert " checked" not in tag_of(response.text, "is_active")
    assert '<option value="de" selected>' in response.text


def test_edit_page_saves_in_place(client, services):
    html = client.get("/sources/1").text

    assert text_of(html, "heading") == "Zentralbank"
    assert 'action="/sources/1"' in tag_of(html, "source-form")
    assert "data-inline-save" in tag_of(html, "source-form")
    assert " checked" not in tag_of(html, "is_active")
    # No language: no option is marked, so the browser shows the first one, "Mehrsprachig oder unbekannt".
    assert " selected" not in re.search(r'<select id="language".*?</select>', html, re.DOTALL).group(0)
    assert ">Nur mit Quellenangabe</textarea>" in html
    # No own minimum interval: empty, not required, and the hint names the platform's value.
    field = tag_of(html, "min_request_interval_seconds-input")
    assert 'value=""' in field and " required" not in field
    assert "Leer: Plattform-Wert (10 s)" in text_of(html, "min_request_interval_seconds-hint")
    assert text_of(html, "fetch-status-label") == "Noch nicht abgerufen"
    assert tag_of(html, "last-error") is None


def test_form_offers_gdelt_with_a_query_and_an_optional_url(client, services):
    html = client.get("/sources/new").text

    assert '<option value="gdelt">GDELT</option>' in html
    assert " required" not in tag_of(html, "query") and 'value=""' in tag_of(html, "query")
    assert " required" not in re.search(r'<input id="url"[^>]*>', html).group(0)

    edit = client.get("/sources/5").text
    assert 'value="&#34;central bank&#34; &lt;sourcelang:german&gt;"' in tag_of(edit, "query")


def test_edit_page_shows_the_fetch_status_and_the_own_minimum_interval(client, services):
    html = client.get("/sources/4").text

    assert text_of(html, "fetch-status-label") == "3 Fehler in Folge"
    assert text_of(html, "last-error") == "HTTP 503 (Retry-After 120 s)"
    assert 'value="60"' in tag_of(html, "min_request_interval_seconds-input")


def test_edit_page_of_a_missing_source_goes_back_to_the_list(client, services):
    text, tag = flash_after(client, client.get("/sources/99"))

    assert text == "Quelle nicht gefunden."
    assert "message-error" in tag


@pytest.mark.parametrize(
    ("source_id", "form", "answer"),
    [
        (1, FORM, {"status": "Gespeichert"}),
        (1, FORM | {"raw_text_retention_days": "400"}, {"error": "Rohtext aufbewahren (Tage): bitte eine ganze Zahl von 0 bis 365 eingeben."}),
        (99, FORM, {"error": "Quelle nicht gefunden."}),
    ],
)
def test_save_in_place_answers_json_with_200(client, services, source_id, form, answer):
    response = client.post(f"/sources/{source_id}", data=form, headers=JSON)

    # 200 also for an invalid entry: browsers log every 4xx answer as a console error.
    assert response.status_code == 200
    assert response.json() == answer
    assert services["update"] == (source_id, form)


def test_save_without_javascript_shows_the_edit_page_with_a_message(client, services):
    response = client.post("/sources/1", data=FORM)

    assert response.headers["location"] == "/sources/1"
    assert flash_after(client, response)[0] == "Gespeichert"


@pytest.mark.parametrize(
    ("path", "call", "flag", "message"),
    [
        ("/sources/delete", "delete", (), "2 Quellen gelöscht."),
        ("/sources/activate", "set_active", (True,), "2 Quellen aktiviert."),
        ("/sources/deactivate", "set_active", (False,), "2 Quellen deaktiviert."),
        ("/sources/fetch", "fetch_now", (), "2 Quellen zum Abruf eingereiht."),
    ],
)
def test_bulk_actions(client, services, path, call, flag, message):
    response = client.post(path, data={"source_id": ["2", "1"]})

    assert services[call] == ([2, 1], *flag)
    assert response.headers["location"] == "/sources"
    text, tag = flash_after(client, response)
    assert text == message
    assert "message-success" in tag


def test_blocked_bulk_action_names_the_blockers_and_does_nothing(client, services):
    services["delete_result"] = BulkResult(processed=0, blocked=["Quelle #9 (nicht gefunden)"])

    text, tag = flash_after(client, client.post("/sources/delete", data={"source_id": ["1", "9"]}))

    assert text == "Nichts gelöscht, blockiert: Quelle #9 (nicht gefunden)."
    assert "message-error" in tag


def test_bulk_action_without_selection(client, services):
    text, _tag = flash_after(client, client.post("/sources/activate", data={}))

    assert services["set_active"] == ([], True)
    assert text == "Keine Quellen ausgewählt."


def test_one_source_in_the_singular(client, services):
    assert flash_after(client, client.post("/sources/delete", data={"source_id": "1"}))[0] == "1 Quelle gelöscht."


def test_navigation_marks_the_sources_page_also_on_the_edit_page(client, services):
    html = client.get("/sources/1").text

    assert 'aria-current="page"' in tag_of(html, "nav-sources")
    assert "aria-current" not in tag_of(html, "nav-dashboard")


def test_settings_page_offers_the_starter_catalogue(client, services):
    html = client.get("/settings/platform").text

    assert text_of(html, "seed-sources-status") == "0 von 17 Quellen importiert."
    assert 'action="/settings/platform/seed/sources"' in tag_of(html, "seed-sources-form")
    assert " disabled" not in tag_of(html, "import-sources")


def test_import_button_is_disabled_once_everything_is_imported(client, services):
    services["seed_status"] = SeedStatus(total=17, imported=17)

    assert " disabled" in tag_of(client.get("/settings/platform").text, "import-sources")


def test_settings_page_load_imports_nothing(client, services):
    client.get("/settings/platform")
    client.get("/sources")

    assert "import" not in services


def test_import_reports_imported_and_skipped(client, services):
    services["import_result"] = SeedResult(imported=3, skipped=14, name_taken=[])

    response = client.post("/settings/platform/seed/sources")

    assert response.headers["location"] == "/settings/platform#starterdaten"
    text, tag = flash_after(client, response)
    assert text == "Starterkatalog Quellen: 3 importiert, 14 schon früher importiert."
    assert "message-success" in tag


def test_import_names_the_sources_whose_name_is_taken(client, services):
    services["import_result"] = SeedResult(imported=16, skipped=0, name_taken=["EZB – Pressemitteilungen"])

    text, tag = flash_after(client, client.post("/settings/platform/seed/sources"))

    assert text == (
        "Starterkatalog Quellen: 16 importiert, 0 schon früher importiert. "
        "Nicht importiert, Name schon vergeben: EZB – Pressemitteilungen."
    )
    assert "message-error" in tag


def test_invalid_source_message_is_the_service_message(client, services, monkeypatch):
    def create(_engine, _form):
        raise InvalidSource("Name: „Neu“ ist bereits vergeben.")

    monkeypatch.setattr(source_service, "create", create)

    response = client.post("/sources", data=FORM)

    assert text_of(response.text, "form-error") == "Name: „Neu“ ist bereits vergeben."


def test_edit_page_has_settings_and_backfill_sections_with_quick_navigation(client, services):
    html = client.get("/sources/5").text

    assert [tag_of(html, f"section-nav-{name}") is not None for name in ("einstellungen", "backfill")] == [True, True]
    assert " open" in tag_of(html, "section-einstellungen") and " open" in tag_of(html, "section-backfill")
    assert 'action="/sources/5/backfills"' in tag_of(html, "backfill-form")
    # GDELT reaches 3 months back; the form says so and suggests the last month.
    assert 'min="' in tag_of(html, "period_start") and "reicht bis" in text_of(html, "period-hint")
    assert " disabled" not in tag_of(html, "start-backfill")
    assert tag_of(html, "backfill-list") is None


def test_new_source_page_has_no_sections(client, services):
    html = client.get("/sources/new").text

    assert tag_of(html, "section-nav") is None and tag_of(html, "backfill-form") is None


def test_a_feed_backfill_has_no_earliest_day_and_an_inactive_source_cannot_start_one(client, services):
    html = client.get("/sources/1").text

    assert 'min="' not in tag_of(html, "period_start")
    assert "Ein Feed liefert nur, was er noch enthält." in text_of(html, "period-hint")
    assert " disabled" in tag_of(html, "start-backfill")


def test_a_source_without_backfill_says_so_instead_of_the_form(client, services, monkeypatch):
    eia = source(6, "EIA Gasspeicher", adapter_type="eia", url="https://api.eia.gov/v2/natural-gas/stor/wkly/data/")
    monkeypatch.setattr(source_service, "get", lambda _engine, source_id: eia)

    html = client.get("/sources/6").text

    assert tag_of(html, "backfill-form") is None
    assert text_of(html, "no-backfill") == "Für Quellen vom Typ EIA gibt es keinen Backfill."


def test_backfills_listed_with_progress_and_only_running_ones_selectable(client, services):
    services["backfills"] = [
        backfill(9, 5),
        backfill(8, 5, status=BackfillStatus.FAILED, last_error="GDELT lehnt die Suchanfrage ab: <zu kurz>",
                 full_texts_pending=0, articles_found=0, articles_new=0, full_texts_stored=0),
    ]

    html = client.get("/sources/5").text

    assert re.findall(r'data-testid="backfill-(\d+)"', html) == ["9", "8"]
    assert text_of(html, "backfill-period-9") == "01.09.2026 bis 30.09.2026"
    assert re.search(r'data-testid="backfill-status-9"><span class="status status-running">Läuft<', html)
    assert re.search(r'data-testid="backfill-progress-9">zurück bis <time datetime="2026-09-20T06:00:00\+00:00"', html)
    assert text_of(html, "backfill-progress-8") == "–"
    assert text_of(html, "backfill-articles-9") == "480 neu von 500"
    assert text_of(html, "backfill-full-texts-9") == "120 von 480 gespeichert, 360 ausstehend"
    assert text_of(html, "backfill-full-texts-8") == "0 von 0 gespeichert"
    assert text_of(html, "backfill-error-8") == "GDELT lehnt die Suchanfrage ab: &lt;zu kurz&gt;"
    assert " disabled" not in tag_of(html, "select-backfill-9") and " disabled" in tag_of(html, "select-backfill-8")
    assert " disabled" in tag_of(html, "cancel-backfills")
    assert 'action="/sources/5/backfills/cancel"' in tag_of(html, "backfill-list")


def test_start_backfill_goes_back_to_its_section_with_a_message(client, services):
    response = client.post("/sources/5/backfills", data={"period_start": "2026-09-01", "period_end": "2026-09-30"})

    assert response.headers["location"] == "/sources/5#backfill"
    assert services["start_backfill"] == (5, {"period_start": "2026-09-01", "period_end": "2026-09-30"})
    assert flash_after(client, response)[0] == "Backfill gestartet: 01.09.2026 bis 30.09.2026."


def test_invalid_backfill_is_named_in_the_message(client, services):
    services["start_backfill_error"] = "Bis: höchstens heute."

    response = client.post("/sources/5/backfills", data={"period_start": "2026-09-01", "period_end": "2099-01-01"})

    text, tag = flash_after(client, response)
    assert text == "Backfill nicht gestartet: Bis: höchstens heute."
    assert "message-error" in tag


@pytest.mark.parametrize(
    ("selected", "result", "message"),
    [
        (["9"], BulkResult(processed=1, blocked=[]), "1 Backfill abgebrochen."),
        (["9", "7"], BulkResult(processed=2, blocked=[]), "2 Backfills abgebrochen."),
        (["8"], BulkResult(processed=0, blocked=["Backfill #8 (Fehlgeschlagen)"]), "Nichts abgebrochen, blockiert: Backfill #8 (Fehlgeschlagen)."),
        ([], BulkResult(processed=0, blocked=[]), "Keine Backfills ausgewählt."),
    ],
)
def test_cancel_backfills(client, services, selected, result, message):
    services["cancel_backfills_result"] = result

    response = client.post("/sources/5/backfills/cancel", data={"backfill_id": selected})

    assert response.headers["location"] == "/sources/5#backfill"
    assert services["cancel_backfills"] == (5, [int(each) for each in selected])
    assert flash_after(client, response)[0] == message


def test_list_names_blocked_full_texts_and_offers_to_fetch_them_again(client, services):
    services["blocked_counts"] = {2: 3, 4: 1}

    html = client.get("/sources").text

    assert text_of(html, "blocked-2") == "3 Volltexte blockiert"
    assert text_of(html, "blocked-4") == "1 Volltext blockiert"
    assert tag_of(html, "blocked-1") is None
    button = tag_of(html, "refetch-blocked")
    assert 'formaction="/sources/refetch-blocked"' in button and " disabled" in button


@pytest.mark.parametrize(
    ("selected", "result", "message"),
    [
        (["2"], BulkResult(processed=3, blocked=[]), "3 blockierte Volltexte erneut eingereiht."),
        (["4"], BulkResult(processed=1, blocked=[]), "1 blockierter Volltext erneut eingereiht."),
        (["1"], BulkResult(processed=0, blocked=[]), "Die ausgewählten Quellen haben keine blockierten Volltexte."),
        (["1"], BulkResult(processed=0, blocked=["Zentralbank (inaktiv)"]), "Nichts eingereiht, blockiert: Zentralbank (inaktiv)."),
        ([], BulkResult(processed=0, blocked=[]), "Keine Quellen ausgewählt."),
    ],
)
def test_refetch_blocked(client, services, selected, result, message):
    services["refetch_blocked_result"] = result

    response = client.post("/sources/refetch-blocked", data={"source_id": selected})

    assert services["refetch_blocked"] == ([int(each) for each in selected],)
    assert flash_after(client, response)[0] == message


def test_list_warns_while_no_contact_is_set(client, services):
    html = client.get("/sources").text

    assert re.search(r'data-testid="contact-missing">Kein Kontakt im User-Agent: Ohne <code>FETCH_CONTACT</code>', html)


def test_no_warning_with_a_contact(services):
    with TestClient(create_app(replace(SETTINGS, fetch_contact="https://github.com/Zickezack"), AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        assert tag_of(client.get("/sources").text, "contact-missing") is None


def test_list_shows_the_region_and_offers_filter_and_sort(client, services):
    services["sources"] = [source(2, "EZB", region="europe"), source(7, "Alt", region=None)]

    html = client.get("/sources").text

    assert (text_of(html, "region-2"), text_of(html, "region-7")) == ("Europa", "Ohne Region")
    assert 'method="get"' in tag_of(html, "source-filter") and 'action="/sources"' in tag_of(html, "source-filter")
    assert services["list_sources"] == (None, None)
    button = tag_of(html, "set-region")
    assert 'formaction="/sources/set-region"' in button and " disabled" in button
    assert 'name="region"' in tag_of(html, "bulk-region")


def test_filter_and_sort_come_from_the_url_and_stay_selected(client, services):
    services["sources_in_region"] = [source(2, "EZB", region="europe")]

    html = client.get("/sources?region=europe&sort=region").text

    assert services["list_sources"] == ("europe", "region")
    assert re.search(r'<option value="europe" selected>Europa</option>', html)
    assert re.search(r'<option value="region" selected>Region</option>', html)
    assert re.findall(r'data-testid="source-(\d+)"', html) == ["2"]


def test_an_empty_selection_offers_all_sources_again(client, services):
    html = client.get("/sources?region=none").text

    assert 'href="/sources"' in html and tag_of(html, "no-sources-in-region") is not None
    assert tag_of(html, "no-sources") is None


@pytest.mark.parametrize(
    ("data", "result", "message"),
    [
        ({"source_id": ["2", "1"], "region": "asia"}, BulkResult(processed=2, blocked=[]), "2 Quellen der Region Asien zugeordnet."),
        ({"source_id": ["9"], "region": "asia"}, BulkResult(processed=0, blocked=["Quelle #9 (nicht gefunden)"]),
         "Nichts der Region Asien zugeordnet, blockiert: Quelle #9 (nicht gefunden)."),
    ],
)
def test_set_region(client, services, monkeypatch, data, result, message):
    seen = {}

    def set_region(_engine, source_ids, region):
        seen["call"] = (source_ids, region)
        return result

    monkeypatch.setattr(source_service, "set_region", set_region)

    response = client.post("/sources/set-region", data=data)

    assert seen["call"] == ([int(each) for each in data["source_id"]], data["region"])
    assert flash_after(client, response)[0] == message


def test_set_region_with_an_unknown_region_says_so(client, services):
    response = client.post("/sources/set-region", data={"source_id": ["2"], "region": "mars"})

    text, tag = flash_after(client, response)
    assert text == "Region: bitte einen der angebotenen Werte wählen." and "message-error" in tag


def test_form_asks_for_a_region(client, services):
    new = client.get("/sources/new").text
    edit = client.get("/sources/2").text

    assert " required" in tag_of(new, "region")
    assert '<option value="" selected disabled>Bitte wählen</option>' in new
    assert '<option value="europe" selected>Europa</option>' in edit


def test_settings_page_takes_regions_from_the_catalogue(client, services):
    services["sources_in_region"] = [source(7, "Alt", region=None)]
    html = client.get("/settings/platform").text

    assert text_of(html, "seed-regions-status") == "1 Quelle ohne Region."
    assert " disabled" not in tag_of(html, "take-regions")

    services["regions_given"] = 31
    response = client.post("/settings/platform/seed/source-regions")
    assert response.headers["location"] == "/settings/platform#starterdaten"
    assert flash_after(client, response)[0] == "31 Quellen haben die Region ihres Katalogeintrags erhalten."


def test_region_takeover_is_disabled_once_every_source_has_one(client, services):
    assert " disabled" in tag_of(client.get("/settings/platform").text, "take-regions")
