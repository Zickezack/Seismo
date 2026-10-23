"""Quellen in PostgreSQL: sort order, unique names, all-or-nothing bulk actions."""
import pytest
from sqlalchemy import func, select

from app.db.models import Source
from app.repositories import source_repository
from app.services import source_service
from app.services.source_service import InvalidSource
from tests.integration.alembic_helpers import upgrade


@pytest.fixture
def database(engine):
    upgrade(engine)
    return engine


def form(name, **fields):
    values = {
        "name": name,
        "adapter_type": "rss",
        "url": "https://example.org/feed.xml",
        "is_active": "on",
        "fetch_interval_minutes": "60",
        "language": "de",
        "terms_note": "",
        "raw_text_retention_days": "30",
    }
    return values | fields


def create(database, *names):
    return [source_service.create(database, form(name)).id for name in names]


def names(database):
    return [source.name for source in source_service.list_sources(database)]


def test_create_stores_every_field(database):
    created = source_service.create(
        database, form("EZB", is_active="", language="", terms_note="Mit Quellenangabe.", raw_text_retention_days="0")
    )

    assert source_service.get(database, created.id) == created
    assert (created.is_active, created.language, created.terms_note, created.raw_text_retention_days) == (
        False, None, "Mit Quellenangabe.", 0
    )
    assert created.created_at == created.updated_at


def test_list_is_sorted_by_name_ignoring_case_with_umlauts_in_place(database):
    create(database, "birne", "Zitrone", "Äpfel", "Apfel", "apfelmus")

    assert names(database) == ["Apfel", "Äpfel", "apfelmus", "birne", "Zitrone"]


def test_name_is_unique_regardless_of_case(database):
    create(database, "EZB")

    with pytest.raises(InvalidSource, match="^Name: „ezb“ ist bereits vergeben.$"):
        source_service.create(database, form("ezb"))

    assert names(database) == ["EZB"]


def test_rename_to_a_name_in_use_changes_nothing(database):
    ezb, fed = create(database, "EZB", "Fed")

    with pytest.raises(InvalidSource, match="^Name: „Ezb“ ist bereits vergeben.$"):
        source_service.update(database, fed, form("Ezb", url="https://example.org/other.xml"))

    assert source_service.get(database, fed).url == "https://example.org/feed.xml"
    assert names(database) == ["EZB", "Fed"]


def test_update_saves_every_field_and_may_change_the_case_of_its_own_name(database):
    [ezb] = create(database, "EZB")
    before = source_service.get(database, ezb)

    updated = source_service.update(
        database, ezb, form("Ezb", fetch_interval_minutes="15", is_active="", language="en", terms_note="Neu.")
    )

    assert (updated.name, updated.fetch_interval_minutes, updated.is_active, updated.language, updated.terms_note) == (
        "Ezb", 15, False, "en", "Neu."
    )
    assert updated.created_at == before.created_at
    assert updated.updated_at > before.updated_at


def test_update_of_a_missing_source(database):
    assert source_service.update(database, 404, form("EZB")) is None


def test_bulk_delete(database):
    ezb, fed, snb = create(database, "EZB", "Fed", "SNB")

    result = source_service.delete(database, [fed, ezb, fed])

    assert (result.processed, result.blocked) == (2, [])
    assert names(database) == ["SNB"]


def test_bulk_delete_with_one_missing_source_deletes_nothing_and_names_it(database):
    ezb, fed = create(database, "EZB", "Fed")

    result = source_service.delete(database, [ezb, 404, fed])

    assert (result.processed, result.blocked) == (0, ["Quelle #404 (nicht gefunden)"])
    assert names(database) == ["EZB", "Fed"]


def test_bulk_activate_and_deactivate(database):
    ezb, fed = create(database, "EZB", "Fed")

    assert source_service.set_active(database, [ezb, fed], False).processed == 2
    assert [source.is_active for source in source_service.list_sources(database)] == [False, False]

    result = source_service.set_active(database, [fed, 404], True)

    assert (result.processed, result.blocked) == (0, ["Quelle #404 (nicht gefunden)"])
    assert source_service.get(database, fed).is_active is False


def test_empty_selection_does_nothing(database):
    create(database, "EZB")

    assert source_service.delete(database, []).processed == 0
    assert names(database) == ["EZB"]


def test_database_rejects_a_second_name_even_past_the_service(database):
    create(database, "EZB")

    with pytest.raises(source_repository.NameTaken), database.begin() as connection:
        source_repository.insert_source(connection, {"name": "eZb", "adapter_type": "rss", "url": "https://example.org"})

    with database.connect() as connection:
        assert connection.execute(select(func.count()).select_from(Source)).scalar_one() == 1


def test_defaults_of_the_table(database):
    with database.begin() as connection:
        source_id = source_repository.insert_source(
            connection, {"name": "Minimal", "adapter_type": "rss", "url": "https://example.org"}
        )

    source = source_service.get(database, source_id)
    assert (source.is_active, source.fetch_interval_minutes, source.raw_text_retention_days, source.language) == (
        True, 60, 30, None
    )
