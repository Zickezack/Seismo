"""Export and import of the configuration against PostgreSQL: a round trip, names in use, all or nothing."""
import json
from datetime import UTC, datetime

import pytest

from app.services import platform_settings_service, source_service, transfer_service
from app.services.transfer_service import InvalidImport
from tests.integration.alembic_helpers import upgrade
from tests.integration.test_sources import create, form, names

NOW = datetime(2026, 10, 29, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


def document(export_file):
    return json.loads(export_file.content)


def test_an_export_names_its_scope_and_carries_the_form_fields_only(database):
    source_service.create(database, form("EZB", url="https://example.org/ecb.xml", quality="high"))
    platform_settings_service.save(database, {"backups_to_keep": "5"})

    exported = transfer_service.export(database, True, True, NOW)

    assert exported.filename == "seismo-einstellungen-quellen-20261029T101500Z.json"
    content = document(exported)
    assert (content["format"], content["version"], content["scope"]) == ("seismo-export", 1, ["platform_settings", "sources"])
    assert content["platform_settings"]["backups_to_keep"] == 5
    [ecb] = content["sources"]
    assert set(ecb) == set(transfer_service.SOURCE_FIELDS)
    assert (ecb["name"], ecb["url"], ecb["quality"], ecb["is_active"]) == ("EZB", "https://example.org/ecb.xml", "high", True)
    assert document(transfer_service.export(database, False, True, NOW))["scope"] == ["sources"]


def test_settings_and_sources_come_back_from_their_export(database):
    create(database, "EZB", "Fed")
    platform_settings_service.save(database, {"backups_to_keep": "5", "extraction_languages": "de, fr"})
    exported = transfer_service.export(database, True, True, NOW)
    original = [source_service.parse(_as_form(source)) for source in source_service.list_sources(database)]
    source_service.delete(database, [source.id for source in source_service.list_sources(database)])
    platform_settings_service.save(database, {"backups_to_keep": "14", "extraction_languages": "en"})

    result = transfer_service.import_file(database, exported.content, True, True)

    assert (result.settings_taken, result.sources_imported, result.name_taken) == (True, 2, [])
    settings = platform_settings_service.current(database)
    assert (settings.backups_to_keep, settings.extraction_languages) == (5, "de,fr")
    imported = [source_service.parse(_as_form(source)) for source in source_service.list_sources(database)]
    assert imported == original


def test_a_source_whose_name_is_in_use_is_skipped_and_a_second_import_changes_nothing(database):
    create(database, "EZB")
    content = transfer_service.export(database, False, True, NOW).content
    source_service.delete(database, [source.id for source in source_service.list_sources(database)])
    create(database, "ezb")
    file = json.loads(content)
    file["sources"].append(file["sources"][0] | {"name": "Fed"})
    content = json.dumps(file).encode()

    first = transfer_service.import_file(database, content, False, True)
    second = transfer_service.import_file(database, content, False, True)

    assert (first.sources_imported, first.name_taken) == (1, ["EZB"])
    assert (second.sources_imported, second.name_taken) == (0, ["EZB", "Fed"])
    assert names(database) == ["ezb", "Fed"]


def test_an_invalid_file_changes_nothing(database):
    create(database, "EZB")
    file = json.loads(transfer_service.export(database, True, True, NOW).content)
    file["platform_settings"]["backups_to_keep"] = 3
    file["sources"] = [file["sources"][0] | {"name": "Neu"}, file["sources"][0] | {"name": "Kaputt", "region": "mars"}]

    with pytest.raises(InvalidImport, match="Quelle 2 .*Region"):
        transfer_service.import_file(database, json.dumps(file).encode(), True, True)

    assert names(database) == ["EZB"]
    assert platform_settings_service.current(database).backups_to_keep == 14


def test_only_the_chosen_part_is_taken(database):
    create(database, "EZB")
    platform_settings_service.save(database, {"backups_to_keep": "5"})
    content = transfer_service.export(database, True, True, NOW).content
    source_service.delete(database, [source.id for source in source_service.list_sources(database)])
    platform_settings_service.save(database, {"backups_to_keep": "14"})

    result = transfer_service.import_file(database, content, False, True)

    assert (result.settings_taken, result.sources_imported) == (False, 1)
    assert platform_settings_service.current(database).backups_to_keep == 14


def test_selected_sources_export_all_or_nothing(database):
    ezb, fed, boj = create(database, "EZB", "Fed", "BoJ")

    result = transfer_service.export_sources(database, [fed, ezb], NOW)

    assert result.file.filename == "seismo-quellen-20261029T101500Z.json"
    assert [source["name"] for source in document(result.file)["sources"]] == ["EZB", "Fed"]
    assert transfer_service.export_sources(database, [boj, 999], NOW) == transfer_service.ExportResult(
        None, ["Quelle #999 (nicht gefunden)"]
    )
    assert transfer_service.export_sources(database, [], NOW) == transfer_service.ExportResult(None, [])


def _as_form(source):
    """A stored source as its form sends it, to compare what parse makes of it."""
    return {
        name: ("on" if value else "") if isinstance(value, bool) else ("" if value is None else str(value))
        for name in transfer_service.SOURCE_FIELDS
        for value in [getattr(source, name)]
    }
