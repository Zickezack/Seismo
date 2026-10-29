"""Import, export and backup files in the web layer, without a database: the backup directory is a temporary one,
the services that read the database are replaced."""
import json
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import AuthSettings, Settings
from app.services import (
    backup_service,
    extraction_service,
    platform_settings_service,
    seed_service,
    source_service,
    transfer_service,
)
from app.services.platform_settings_service import PlatformSettings
from app.services.seed_service import SeedStatus
from app.services.transfer_service import ExportFile, ExportResult, ImportResult
from app.web.main import create_app

SETTINGS = Settings(postgres_user="u", postgres_password="p", postgres_db="d")
AUTH = AuthSettings(admin_username="admin", admin_password="correct horse battery", session_secret="s" * 32)
EXPORT = ExportFile("seismo-quellen-20261029T101500Z.json", b'{"format": "seismo-export"}')


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path


@pytest.fixture
def client(backup_dir, monkeypatch):
    monkeypatch.setattr(platform_settings_service, "current", lambda _engine: PlatformSettings())
    monkeypatch.setattr(seed_service, "sources_status", lambda _engine: SeedStatus(total=1, imported=0))
    monkeypatch.setattr(source_service, "list_sources", lambda _engine, region=None, sort=None: [])
    monkeypatch.setattr(extraction_service, "llm_status", lambda *_llm: None)
    with TestClient(create_app(replace(SETTINGS, backup_dir=backup_dir), AUTH), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        yield client


def flash_of(client):
    match = re.search(r'data-testid="flash"[^>]*>([^<]*)<', client.get("/settings/platform").text)
    return match.group(1).strip() if match else None


def tag_of(html, testid):
    match = re.search(rf'<[^>]*data-testid="{testid}"[^>]*>', html)
    return match.group(0) if match else None


def test_the_export_is_a_download_of_the_chosen_parts(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        transfer_service, "export", lambda _engine, settings, sources: calls.append((settings, sources)) or EXPORT
    )

    response = client.get("/settings/platform/export", params={"quellen": "on"})

    assert calls == [(False, True)]
    assert response.status_code == 200 and response.content == EXPORT.content
    assert response.headers["content-disposition"] == f'attachment; filename="{EXPORT.filename}"'
    assert response.headers["content-type"] == "application/json"


def test_an_export_of_nothing_says_so(client):
    response = client.get("/settings/platform/export")

    assert response.headers["location"] == "/settings/platform#import-export"
    assert flash_of(client) == "Bitte Einstellungen, Quellen oder beides wählen."


def test_an_import_passes_the_file_and_the_choice_and_reports_the_result(client, monkeypatch):
    calls = []

    def fake(_engine, content, take_settings, take_sources):
        calls.append((content, take_settings, take_sources))
        return ImportResult(True, 2, ["EZB"])

    monkeypatch.setattr(transfer_service, "import_file", fake)
    response = client.post(
        "/settings/platform/import",
        data={"einstellungen": "on", "quellen": "on"},
        files={"file": ("export.json", b'{"x": 1}', "application/json")},
    )

    assert calls == [(b'{"x": 1}', True, True)]
    assert response.headers["location"] == "/settings/platform#import-export"
    assert flash_of(client) == (
        "Import: Plattform-Einstellungen übernommen, 2 Quellen angelegt. Übersprungen, Name schon vergeben: EZB."
    )


def test_an_invalid_import_file_changes_nothing_and_names_why(client):
    client.post("/settings/platform/import", data={"quellen": "on"}, files={"file": ("x.json", b"{kaputt", "application/json")})

    assert flash_of(client) == "Nichts importiert. Die Datei ist kein JSON, kein Export von Seismo."


def test_selected_sources_download_and_a_missing_one_blocks_the_export(client, monkeypatch):
    results = iter([ExportResult(EXPORT, []), ExportResult(None, ["Quelle #9 (nicht gefunden)"])])
    calls = []
    monkeypatch.setattr(transfer_service, "export_sources", lambda _engine, ids: calls.append(ids) or next(results))

    response = client.post("/sources/export", data={"source_id": ["2", "4"]})
    assert calls == [[2, 4]] and response.content == EXPORT.content
    assert response.headers["content-disposition"] == f'attachment; filename="{EXPORT.filename}"'

    response = client.post("/sources/export", data={"source_id": ["9"]})
    assert response.headers["location"] == "/sources"


def test_the_backup_list_downloads_a_listed_file_and_nothing_else(client, backup_dir):
    (backup_dir / "d-20261023T030000Z.dump").write_bytes(b"PGDMP made here")
    (backup_dir / "secret.txt").write_text("not for download")

    html = client.get("/settings/platform").text
    assert 'href="/settings/platform/backups/d-20261023T030000Z.dump"' in tag_of(html, "download-backup-1")
    assert tag_of(html, "download-backup-2") is None

    response = client.get("/settings/platform/backups/d-20261023T030000Z.dump")
    assert response.status_code == 200 and response.content == b"PGDMP made here"
    assert 'filename="d-20261023T030000Z.dump"' in response.headers["content-disposition"]
    response = client.get("/settings/platform/backups/secret.txt")
    assert response.headers["location"] == "/settings/platform#backup-dateien"
    # A path does not match the route at all.
    response = client.get("/settings/platform/backups/..%2Fsecret.txt")
    assert response.status_code == 404 and b"not for download" not in response.content


def test_an_uploaded_backup_lands_in_the_backup_directory_and_another_file_does_not(client, backup_dir):
    client.post("/settings/platform/backups/upload", files={"file": ("x.dump", b"PGDMP dump", "application/octet-stream")})
    [uploaded] = list(backup_dir.iterdir())
    assert re.fullmatch(r"d-upload-\d{8}T\d{6}Z\.dump", uploaded.name)
    assert flash_of(client) == f"Hochgeladen als {uploaded.name}."

    client.post("/settings/platform/backups/upload", files={"file": ("x.sql", b"SELECT 1;", "text/plain")})
    assert flash_of(client).startswith("Nicht hochgeladen. Die Datei ist kein Backup von pg_dump")
    assert list(backup_dir.iterdir()) == [uploaded]


def test_deleting_backups_reports_the_result(client, backup_dir):
    (backup_dir / "d-20261023T030000Z.dump").write_bytes(b"PGDMP")

    client.post("/settings/platform/backups/delete", data={"backup": ["d-20261023T030000Z.dump", "secret.txt"]})
    assert flash_of(client) == "Nichts gelöscht, blockiert: secret.txt (nicht gefunden)."

    client.post("/settings/platform/backups/delete", data={"backup": ["d-20261023T030000Z.dump"]})
    assert flash_of(client) == "1 Backup gelöscht."
    assert list(backup_dir.iterdir()) == []


def test_a_backup_now_is_queued_once(client, monkeypatch):
    queued = iter([17, None])
    monkeypatch.setattr(backup_service, "enqueue_now", lambda _engine: next(queued))

    client.post("/settings/platform/backups/create")
    assert flash_of(client) == "Backup eingereiht: der Worker legt es an, Status unter Jobs."
    client.post("/settings/platform/backups/create")
    assert flash_of(client) == "Ein Backup ist schon eingereiht oder läuft."


def test_the_settings_page_says_when_the_backup_directory_is_out_of_reach(client, backup_dir):
    assert tag_of(client.get("/settings/platform").text, "backup-dir-missing") is None
    backup_dir.rmdir()
    assert tag_of(client.get("/settings/platform").text, "backup-dir-missing") is not None
