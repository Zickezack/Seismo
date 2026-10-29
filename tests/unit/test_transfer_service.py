"""The export file's check, without a database: an invalid file is refused before anything is written.

Export, import and the name rules against PostgreSQL: tests/integration/test_transfer.py.
"""
import json

import pytest

from app.services import transfer_service
from app.services.transfer_service import InvalidImport

SOURCE = {
    "name": "EZB", "adapter_type": "rss", "url": "https://example.org/feed.xml", "query": None, "is_active": True,
    "extract_events": False, "fetch_interval_minutes": 60, "language": "de", "terms_note": None,
    "raw_text_retention_days": 30, "min_request_interval_seconds": None, "region": "europe", "quality": "high",
}


def file(**parts):
    return json.dumps({"format": "seismo-export", "version": 1, "scope": [], **parts}).encode()


class NoDatabase:
    """Fails the test the moment an import would write."""

    def begin(self):
        raise AssertionError("an invalid file must not reach the database")


def refused(content, take_settings=True, take_sources=True):
    with pytest.raises(InvalidImport) as error:
        transfer_service.import_file(NoDatabase(), content, take_settings, take_sources)
    return str(error.value)


@pytest.mark.parametrize("content, message", [
    (b"\xff\xfe", "kein JSON"),
    (b"{not json", "kein JSON"),
    (b"[]", "kein Export von Seismo"),
    (json.dumps({"format": "other", "version": 1}).encode(), "kein Export von Seismo"),
    (json.dumps({"format": "seismo-export", "version": 2}).encode(), "Version 2 wird nicht unterstützt"),
    (b" " * (transfer_service.MAX_FILE_BYTES + 1), "grösser als 5 MB"),
])
def test_a_file_that_is_no_export_is_refused(content, message):
    assert message in refused(content)


def test_neither_part_chosen_is_refused():
    assert refused(file(sources=[SOURCE]), take_settings=False, take_sources=False) == (
        "Bitte Einstellungen, Quellen oder beides wählen."
    )


def test_a_chosen_part_the_file_lacks_is_refused():
    assert refused(file(sources=[SOURCE]), take_sources=False) == "Die Datei enthält keine Plattform-Einstellungen."
    assert refused(file(platform_settings={}), take_settings=False) == "Die Datei enthält keine Quellen."


def test_every_invalid_source_is_named_and_the_valid_ones_are_not_written():
    content = file(sources=[
        SOURCE,
        SOURCE | {"name": "Fed", "url": "ftp://example.org", "fetch_interval_minutes": 1},
        "EZB",
        SOURCE | {"name": "Schlüssel", "url": "https://example.org/feed?api_key=geheim"},
        SOURCE | {"name": "Neu", "colour": "red"},
        SOURCE | {"name": "Liste", "language": ["de"]},
    ])

    message = refused(content, take_settings=False)

    assert message.startswith("Quelle 2 („Fed“): URL")
    assert "Abrufintervall (Minuten)" in message
    assert "Quelle 3: kein Objekt." in message
    assert "Quelle 4 („Schlüssel“): " in message
    assert "Quelle 5 („Neu“): unbekannt colour." in message
    assert "Quelle 6 („Liste“): kein Text, keine Zahl und kein Wahrheitswert: language." in message


def test_a_name_twice_in_the_file_regardless_of_case_is_refused():
    content = file(sources=[SOURCE, SOURCE | {"name": " ezb "}])

    assert refused(content, take_settings=False) == "Quelle 2 („ezb“): der Name kommt schon als Quelle 1 vor."


def test_a_source_takes_the_new_form_s_defaults_for_missing_fields():
    values = transfer_service._source({"name": "Minimal", "url": "https://example.org/f.xml", "region": "asia"}, "Q")

    assert (values.adapter_type, values.is_active, values.extract_events) == ("rss", True, True)
    assert (values.fetch_interval_minutes, values.raw_text_retention_days, values.language) == (60, 30, None)


def test_invalid_or_unknown_settings_are_refused():
    assert refused(file(platform_settings={"backups_to_keep": 0}), take_sources=False).startswith(
        "Plattform-Einstellungen: Backups aufbewahren (Anzahl): bitte eine ganze Zahl von 1 bis 365"
    )
    assert refused(file(platform_settings={"admin_password": "x"}), take_sources=False) == (
        "Plattform-Einstellungen: unbekannt admin_password."
    )
    assert refused(file(platform_settings=[]), take_sources=False) == "Plattform-Einstellungen: kein Objekt."
