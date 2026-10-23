"""Quellen pages and the starter catalogue import in the web layer, without a database: the services are replaced.

Logins and the 401 for every route without a session: tests/unit/test_web_auth.py.
"""
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.services import platform_settings_service, seed_service, source_service
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
        id=source_id, name=name, adapter_type="rss", url=f"https://example.org/{source_id}.xml", is_active=True,
        fetch_interval_minutes=60, language="en", terms_note=None, raw_text_retention_days=30, created_at=NOW,
        updated_at=NOW,
    )
    return Source(**values | fields)


SOURCES = [
    source(2, "Apfel <Feed>"),
    source(1, "Zentralbank", is_active=False, language=None, raw_text_retention_days=1, terms_note="Nur mit Quellenangabe"),
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

    monkeypatch.setattr(source_service, "list_sources", lambda _engine: calls["sources"])
    monkeypatch.setattr(source_service, "get", lambda _engine, source_id: found.get(source_id))
    monkeypatch.setattr(source_service, "create", create)
    monkeypatch.setattr(source_service, "update", update)
    monkeypatch.setattr(source_service, "delete", bulk("delete"))
    monkeypatch.setattr(source_service, "set_active", bulk("set_active"))
    monkeypatch.setattr(seed_service, "import_sources", import_sources)
    monkeypatch.setattr(seed_service, "sources_status", lambda _engine: calls.get("seed_status", SeedStatus(17, 0)))
    monkeypatch.setattr(platform_settings_service, "current", lambda _engine: PlatformSettings())
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


def test_list_shows_the_sources_in_the_order_the_service_returns(client, services):
    html = client.get("/sources").text

    assert re.findall(r'data-testid="source-(\d+)"', html) == ["2", "1"]
    assert text_of(html, "name-2") == "Apfel &lt;Feed&gt;"
    assert text_of(html, "url-2") == "https://example.org/2.xml"
    assert text_of(html, "status-2") == "Aktiv"
    assert text_of(html, "status-1") == "Inaktiv"
    assert text_of(html, "language-2") == "Englisch"
    assert text_of(html, "language-1") == "Mehrsprachig oder unbekannt"
    assert text_of(html, "interval-2") == "alle 60 Min."
    assert text_of(html, "retention-2") == "30 Tage"
    assert text_of(html, "retention-1") == "1 Tag"


def test_bulk_buttons_start_disabled_and_delete_asks_first(client, services):
    html = client.get("/sources").text

    for action in ("activate", "deactivate", "delete"):
        assert " disabled" in tag_of(html, action)
    assert 'data-confirm-delete="Quelle|Quellen"' in tag_of(html, "delete")
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
