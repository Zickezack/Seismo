"""The filter of the event list and the start page in PostgreSQL (userstories.md, Dashboard, Recherche und Alerts,
story 2): each condition in the query, an event matching what it or one of its reports states."""
from datetime import timedelta

import pytest
from sqlalchemy import insert

from app.db.models import Entity, EntityAlias, EntityMention
from app.repositories import event_repository
from app.repositories.event_repository import CHANGED, DEVELOPING, NEW, EventFilter
from app.services import dashboard_service, event_service
from tests.integration.test_dashboard import FLOOD, HOURS, STRIKE
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_events import QUAKE, RATES, SAME_QUAKE, assign_all, assigned, reported
from tests.integration.test_extraction import Ollama
from tests.integration.test_ingestion import NOW


def impact(target_kind, horizon):
    return {"target": "x", "target_kind": target_kind, "direction": "negative", "horizon": horizon, "description": "", "quote": "x"}


def named(database, candidate_id, kind, name, *name_forms, position=0):
    """An entity of `kind` the report names as an actor, by the last of its name forms."""
    with database.begin() as connection:
        entity_id = connection.execute(insert(Entity).values(kind=kind, name=name).returning(Entity.id)).scalar_one()
        alias_ids = [
            connection.execute(insert(EntityAlias).values(
                entity_id=entity_id, scheme="name", value=value, key=value.lower(), match="founding",
            ).returning(EntityAlias.id)).scalar_one()
            for value in (name, *name_forms)
        ]
        connection.execute(insert(EntityMention).values(candidate_id=candidate_id, alias_id=alias_ids[-1], source="actor", position=position))
    return entity_id


@pytest.fixture
def world(database):
    """Four events by name, and a sector the flood names: a quake escalating within the last day, a strike continuing, rates new within it - its country
    stated only by its later report -, and a flood of three days ago."""
    [quake] = reported(database, "a", QUAKE | {"status": "start"}, published_at=NOW - 48 * HOURS)
    reported(database, "b", SAME_QUAKE | {"status": "escalation"}, published_at=NOW - HOURS)
    [strike] = reported(database, "c", STRIKE | {"status": "update"}, published_at=NOW - 30 * HOURS)
    reported(database, "d", STRIKE | {"status": "update", "title": "Dock workers in Rotterdam continue their strike",
                                      "impacts": [impact("company", "days")]}, published_at=NOW - 3 * HOURS)
    [rates] = reported(database, "e", RATES | {"status": "start", "impacts": [impact("market", "weeks")]}, published_at=NOW - 2 * HOURS)
    reported(database, "f", RATES | {"status": "update", "country": "DE"}, published_at=NOW - HOURS)
    [flood] = reported(database, "g", FLOOD | {"status": "start", "impacts": [impact("country", "years")]}, published_at=NOW - 72 * HOURS)
    sector = named(database, flood, "sector", "Transport und Logistik")
    named(database, flood, "company", "Adif", "Adif SA", position=1)
    named(database, rates, "authority", "European Central Bank", "ECB")
    assign_all(database, Ollama(status=503))
    events = {name: assigned(database)[candidate].event_id
              for name, candidate in (("quake", quake), ("strike", strike), ("rates", rates), ("flood", flood))}
    return events, sector


def matching(database, world, **criteria):
    """The names of the events that match, as the list shows them, and checked against its count."""
    criteria = EventFilter(**criteria)
    overview = event_service.overview(database, criteria)
    assert overview.total == len(overview.events)
    names = {event_id: name for name, event_id in world[0].items()}
    return {names[event.id] for event in overview.events}


def test_without_a_filter_every_event_is_listed(database, world):
    assert matching(database, world) == {"quake", "strike", "rates", "flood"}


@pytest.mark.parametrize(("country", "expected"), [("ES", {"flood"}), ("CN", {"quake"}), ("DE", {"rates"}), ("FR", set())])
def test_a_country_matches_the_event_or_one_of_its_reports(database, world, country, expected):
    """The rates' own country is unknown (its founding report states none); its later report states Germany."""
    assert matching(database, world, country=country) == expected


def test_the_countries_offered_are_those_stated(database, world):
    with database.connect() as connection:
        assert event_repository.countries(connection) == ["CN", "DE", "ES", "NL"]


def test_a_type_sector_and_company(database, world):
    assert matching(database, world, event_type="labor") == {"strike"}
    assert matching(database, world, sector_id=world[1]) == {"flood"}
    # By a part of any of its name forms, regardless of case - the report named it by another.
    assert matching(database, world, company="adif") == {"flood"}
    assert matching(database, world, company="ADIF sa") == {"flood"}
    # An authority is no company.
    assert matching(database, world, company="ecb") == set()


def test_relevance_by_the_sources_and_an_economic_impact(database, world):
    assert matching(database, world, min_sources=2) == {"quake", "strike", "rates"}
    assert matching(database, world, min_sources=3) == set()
    # An impact on a company or a market; on a country it is none.
    assert matching(database, world, impact_kinds=event_service.ECONOMIC_TARGETS) == {"strike", "rates"}


@pytest.mark.parametrize(("horizon", "expected"), [("days", {"strike"}), ("weeks", {"rates"}), ("years", {"flood"}), ("months", set())])
def test_a_horizon_of_an_impact_a_report_names(database, world, horizon, expected):
    assert matching(database, world, horizon=horizon) == expected


def test_new_developing_and_changed_within_a_period(database, world):
    since = NOW - 24 * HOURS
    assert matching(database, world, since=since) == {"quake", "strike", "rates"}
    assert matching(database, world, since=since, development=NEW) == {"rates"}
    assert matching(database, world, since=since, development=DEVELOPING) == {"quake", "strike"}
    # The quake escalated within the day; the strike's update repeats its state - no change.
    assert matching(database, world, since=since, development=CHANGED) == {"quake"}
    assert matching(database, world, since=NOW - 4 * 24 * HOURS, development=NEW) == {"quake", "strike", "rates", "flood"}


def test_conditions_combine(database, world):
    assert matching(database, world, min_sources=2, impact_kinds=event_service.ECONOMIC_TARGETS, horizon="weeks") == {"rates"}


def test_the_start_page_shows_only_the_events_that_match(database, world):
    board = dashboard_service.overview(database, "24h", now=NOW, criteria=EventFilter(impact_kinds=event_service.ECONOMIC_TARGETS))

    events, _ = world
    assert [item.event.id for item in board.new] == [events["rates"]]
    assert [item.event.id for item in board.developing] == [events["strike"]]
    assert (board.new_total, board.developing_total, board.changed_total) == (1, 1, 0)


def test_the_filter_of_the_url_reaches_the_query(database, world):
    form = event_service.parse_filter({"period": "24h", "development": "changed", "sources": "2"}, now=NOW)

    assert [event.id for event in event_service.overview(database, form.criteria).events] == [world[0]["quake"]]
