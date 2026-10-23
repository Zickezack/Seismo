"""Starter catalogue of Quellen: only on request, each item once (architecture.md, "Seed Data")."""
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.config import AuthSettings
from app.db.models import SeedLogEntry, Source
from app.services import seed_service, source_service
from app.services.seed_service import SeedItem
from app.web.main import create_app
from tests.integration.alembic_helpers import upgrade

AUTH = AuthSettings(admin_username="admin", admin_password="correct horse battery", session_secret="s" * 32)


def item(key, name, **fields):
    entry = {"name": name, "url": f"https://example.org/{key}", "region": "europe"} | fields
    return SeedItem(key=key, values=source_service.parse(seed_service._form_of(entry)))


CATALOGUE = [item("ecb", "EZB"), item("fed", "Fed"), item("snb", "SNB")]


@pytest.fixture
def catalogue(monkeypatch):
    """The catalogue the import reads; a test adds items to it, as a later version of the seed file would."""
    items = list(CATALOGUE)
    monkeypatch.setattr(seed_service, "source_catalogue", lambda: items)
    return items


@pytest.fixture
def database(engine, catalogue):
    upgrade(engine)
    return engine


def names(database):
    return [source.name for source in source_service.list_sources(database)]


def count(database, table):
    with database.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_two_runs_in_a_row_create_no_duplicates(database):
    first = seed_service.import_sources(database)
    second = seed_service.import_sources(database)

    assert (first.imported, first.skipped, second.imported, second.skipped) == (3, 0, 0, 3)
    assert names(database) == ["EZB", "Fed", "SNB"]
    assert all(source.is_active for source in source_service.list_sources(database))


def test_renamed_item_is_not_recreated(database):
    seed_service.import_sources(database)
    ezb = next(source for source in source_service.list_sources(database) if source.name == "EZB")
    source_service.update(database, ezb.id, {**_form(ezb), "name": "Europäische Zentralbank"})

    result = seed_service.import_sources(database)

    assert result.imported == 0
    assert names(database) == ["Europäische Zentralbank", "Fed", "SNB"]


def test_deleted_item_is_not_recreated(database):
    seed_service.import_sources(database)
    fed = next(source for source in source_service.list_sources(database) if source.name == "Fed")
    source_service.delete(database, [fed.id])

    result = seed_service.import_sources(database)

    assert result.imported == 0
    assert names(database) == ["EZB", "SNB"]


def test_item_added_to_the_catalogue_later_comes_with_the_next_run(database, catalogue):
    seed_service.import_sources(database)
    catalogue.append(item("boe", "Bank of England"))

    result = seed_service.import_sources(database)

    assert (result.imported, result.skipped) == (1, 3)
    assert names(database) == ["Bank of England", "EZB", "Fed", "SNB"]
    assert seed_service.sources_status(database) == seed_service.SeedStatus(total=4, imported=4)


def test_name_in_use_is_reported_and_imported_once_the_name_is_free(database):
    own = source_service.create(database, seed_service._form_of({"name": "fed", "url": "https://example.org/own", "region": "europe"}))

    result = seed_service.import_sources(database)

    assert (result.imported, result.name_taken) == (2, ["Fed"])
    assert seed_service.sources_status(database) == seed_service.SeedStatus(total=3, imported=2)

    source_service.update(database, own.id, {**_form(own), "name": "Eigene Fed"})
    result = seed_service.import_sources(database)

    assert (result.imported, result.skipped, result.name_taken) == (1, 2, [])
    assert names(database) == ["Eigene Fed", "EZB", "Fed", "SNB"]


def test_concurrent_runs_import_each_item_exactly_once(database):
    results = []
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        results.append(seed_service.import_sources(database))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result.imported for result in results) == 3
    assert all(result.name_taken == [] for result in results)
    assert names(database) == ["EZB", "Fed", "SNB"]
    assert count(database, SeedLogEntry) == 3


def test_app_start_and_page_loads_create_no_seed_rows(database, test_settings):
    with TestClient(create_app(test_settings, AUTH)) as client:
        client.post("/login", data={"username": "admin", "password": "correct horse battery"})
        assert client.get("/settings/platform").status_code == 200
        assert client.get("/sources").status_code == 200

    assert count(database, Source) == 0
    assert count(database, SeedLogEntry) == 0


def test_shipped_catalogue_imports_completely(engine):
    upgrade(engine)

    result = seed_service.import_sources(engine)

    assert result.imported == len(seed_service.source_catalogue())
    assert result.name_taken == []


def _form(source):
    """The edit form of a source as the browser sends it."""
    return {
        "name": source.name,
        "adapter_type": source.adapter_type,
        "url": source.url,
        "is_active": "on" if source.is_active else "",
        "fetch_interval_minutes": str(source.fetch_interval_minutes),
        "language": source.language or "",
        "region": source.region or "",
        "terms_note": source.terms_note or "",
        "raw_text_retention_days": str(source.raw_text_retention_days),
    }


def test_catalogue_regions_fill_only_missing_ones_known_by_url_and_query(database, catalogue):
    gdelt = {"adapter_type": "gdelt", "url": "", "language": ""}
    catalogue += [
        item("gdelt-conflict", "GDELT Konflikte", query="(airstrike OR invasion)", region="worldwide", **gdelt),
        item("gdelt-asia", "GDELT Asien", query="sourcecountry:japan", region="asia", **gdelt),
    ]
    seed_service.import_sources(database)
    ids = {source.name: source.id for source in source_service.list_sources(database)}
    form = {"name": "Eigene", "adapter_type": "rss", "url": "https://own.example/feed", "is_active": "on",
            "fetch_interval_minutes": "60", "raw_text_retention_days": "30", "region": "europe"}
    other = source_service.create(database, form).id
    with database.begin() as connection:
        # As before regions existed; the admin has given one of them a region of their own since.
        connection.execute(update(Source).values(region=None))
        connection.execute(update(Source).where(Source.id == ids["Fed"]).values(region="north_america"))

    given = seed_service.take_catalogue_regions(database)

    regions = {source.name: source.region for source in source_service.list_sources(database)}
    assert given == 4
    assert regions == {
        "EZB": "europe", "Fed": "north_america", "SNB": "europe",
        # One URL for both: each known by its query.
        "GDELT Konflikte": "worldwide", "GDELT Asien": "asia",
        "Eigene": None,  # not in the catalogue
    }
    assert other and seed_service.take_catalogue_regions(database) == 0
