"""Historical search, uncertainty and comparable earlier events in PostgreSQL (userstories.md, Dashboard, Recherche und
Alerts, story 4)."""
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, text, update

from app.db.models import EntityAlias, EntityMention, Event, EventCandidate
from app.repositories import event_repository, extraction_repository
from app.repositories.event_repository import EventFilter
from app.services import event_context_service, event_correction_service, event_service
from tests.integration.test_dashboard import HOURS, STRIKE
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_event_filter import named
from tests.integration.test_events import QUAKE, RATES, SAME_QUAKE, assign_all, assigned
from tests.integration.test_extraction import Ollama, source
from tests.integration.test_ingestion import NOW
from tests.integration.test_normalization import article

DAYS = timedelta(days=1)


def statement(kind, text, confidence="high", quality="high"):
    return {"kind": kind, "text": text, "quote": text, "confidence": confidence, "source_quality": quality}


def reported(database, key, report, statements=(), inference=None, published_at=NOW):
    """An article of `published_at` whose extraction found the report with these statements; its candidate id."""
    article_id = article(database, source(database, f"Q{key}"), key, published_at=published_at, url=f"https://news.example/{key}.html")
    candidate = {
        "event_type": report["event_type"], "status": report.get("status", "start"), "title": report["title"],
        "summary": report["summary"], "country": report["country"], "place": report["place"],
        "actors": [{"name": name, "role": "", "quote": name} for name in report["actors"]],
        "impacts": report.get("impacts", []), "inference_confidence": inference, "statements": list(statements),
    }
    values = {"article_id": article_id, "model": "m", "schema_version": "2", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        extraction_id = extraction_repository.insert_extraction(connection, values, [candidate])
        return connection.execute(select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id)).scalar_one()


def event_of(database, candidate_id):
    assign_all(database, Ollama(status=503))
    return assigned(database)[candidate_id].event_id


def impact(target, direction):
    return {"target": target, "target_kind": "company", "direction": direction, "horizon": "days", "description": "", "quote": target}


def listed(database, **criteria):
    return {event.id for event in event_service.overview(database, EventFilter(**criteria)).events}


# The historical search.

@pytest.fixture
def history(database):
    """A quake a year ago with a statement on its aftershocks, a strike a month ago, the rates today."""
    quake = reported(database, "a", QUAKE, [statement("fact", "Several aftershocks followed the main quake.")], published_at=NOW - 365 * DAYS)
    strike = reported(database, "b", STRIKE, published_at=NOW - 30 * DAYS)
    rates = reported(database, "c", RATES)
    assign_all(database, Ollama(status=503))
    found = assigned(database)
    return {name: found[candidate].event_id for name, candidate in (("quake", quake), ("strike", strike), ("rates", rates))}


def test_the_search_finds_english_word_forms_in_titles_summaries_and_statements(database, history):
    # "earthquakes" finds "Earthquake", "strikes" the dock workers' "strike" and the quake's title "strikes".
    assert listed(database, text="earthquakes") == {history["quake"]}
    assert listed(database, text="strikes") == {history["quake"], history["strike"]}
    # Only in a statement.
    assert listed(database, text="aftershock") == {history["quake"]}
    assert listed(database, text="interest rates") == {history["rates"]}


def test_the_search_takes_quoted_phrases_or_and_exclusions(database, history):
    assert listed(database, text='"key interest"') == {history["rates"]}
    assert listed(database, text='"interest key"') == set()
    assert listed(database, text="earthquake or rotterdam") == {history["quake"], history["strike"]}
    assert listed(database, text="strikes -rotterdam") == {history["quake"]}
    # Stop words only: nothing, never everything.
    assert listed(database, text="the of") == set()


def test_a_period_of_the_past_has_a_start_and_an_end(database, history):
    assert listed(database, since=NOW - 400 * DAYS, until=NOW - 300 * DAYS) == {history["quake"]}
    assert listed(database, until=NOW - 10 * DAYS) == {history["quake"], history["strike"]}
    assert listed(database, since=NOW - 40 * DAYS, until=NOW - 10 * DAYS, text="strike") == {history["strike"]}


def test_the_search_uses_its_full_text_indexes(database, history):
    """The query states the indexes' expressions: PostgreSQL would read every report and statement otherwise."""
    query = event_repository._listed(EventFilter(text="earthquake")).compile(database, compile_kwargs={"literal_binds": True})
    with database.connect() as connection:
        connection.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(row[0] for row in connection.execute(text(f"EXPLAIN {query}")))
    assert "ix_event_candidate_search" in plan and "ix_statement_search" in plan


# Uncertainty.

def test_the_uncertainty_counts_kinds_and_levels_and_finds_the_contradictions(database):
    first = reported(database, "a", QUAKE | {"status": "closure", "impacts": [impact("Xizang Rail", "negative")]}, [
        statement("fact", "The quake killed 126 people."),
        statement("claim", "Officials say rescue is complete.", confidence="medium", quality=None),
    ], inference="high", published_at=NOW - 2 * HOURS)
    second = reported(database, "b", SAME_QUAKE | {"status": "start", "impacts": [impact("xizang rail", "positive")]}, [
        statement("interpretation", "Rebuilding may take years.", confidence="low"),
    ], published_at=NOW - HOURS)
    event_id = event_of(database, first)
    assert assigned(database)[second].event_id == event_id
    _uncertain_form(database, second, "Xizang Railways Co")

    uncertainty = event_context_service.uncertainty_of(database, [event_id])[event_id]
    assert (uncertainty.reports, uncertainty.statements) == (2, 3)
    assert uncertainty.kinds_label == "1 Tatsache, 1 Behauptung, 1 Interpretation"
    assert uncertainty.extraction.counts == (1, 1, 1, 0)
    assert uncertainty.source_quality.counts == (2, 0, 0, 1)
    assert uncertainty.inference.counts == (1, 0, 0, 1)
    # A start after a closure contradicts it; the rail line is seen both ways, by its name in any case.
    assert [report.id for report in uncertainty.contradicting] == [second]
    [conflict] = uncertainty.impact_conflicts
    assert (conflict.target, [r.id for r in conflict.positive], [r.id for r in conflict.negative]) == ("Xizang Rail", [second], [first])
    assert [form.value for form in uncertainty.uncertain_forms] == ["Xizang Railways Co"]
    assert uncertainty.llm_assigned == 0


def _uncertain_form(database, candidate_id, value):
    entity_id = named(database, candidate_id, "company", "Xizang Railway")
    with database.begin() as connection:
        alias_id = connection.execute(insert(EntityAlias).values(
            entity_id=entity_id, scheme="name", value=value, key=value.lower(), match="llm", similarity=0.5, uncertain=True,
        ).returning(EntityAlias.id)).scalar_one()
        connection.execute(insert(EntityMention).values(candidate_id=candidate_id, alias_id=alias_id, source="actor", position=1))
    return alias_id


def test_an_impact_on_one_entity_named_two_ways_is_one_target(database):
    first = reported(database, "a", QUAKE | {"impacts": [impact("Adif", "negative")]}, published_at=NOW - HOURS)
    second = reported(database, "b", SAME_QUAKE | {"impacts": [impact("Adif SA", "positive"), impact("Renfe", "mixed")]})
    event_id = event_of(database, first)
    entity_id = named(database, first, "company", "Adif")
    with database.begin() as connection:
        alias_id = connection.execute(select(EntityAlias.id).where(EntityAlias.entity_id == entity_id)).scalar_one()
        connection.execute(update(EntityMention).where(EntityMention.candidate_id == first).values(source="impact"))
        connection.execute(insert(EntityMention).values(candidate_id=second, alias_id=alias_id, source="impact", position=0))

    [conflict] = event_context_service.uncertainty_of(database, [event_id])[event_id].impact_conflicts
    assert (conflict.target, conflict.entity_id) == ("Adif", entity_id)


def test_an_event_without_reports_has_nothing_uncertain(database):
    first = reported(database, "a", QUAKE, published_at=NOW - HOURS)
    event_id = event_of(database, first)
    other = reported(database, "b", RATES)
    other_event = event_of(database, other)
    event_correction_service.merge(database, other_event, [event_id])
    uncertainty = event_context_service.uncertainty_of(database, [event_id])[event_id]
    assert (uncertainty.reports, uncertainty.statements, uncertainty.contradictions) == (0, 0, 0)


# Comparable earlier events.

OLD_QUAKE = QUAKE | {"title": "Earthquake of magnitude 5.9 strikes Xizang", "summary": "A quake hit Dingri county in Xizang, damaging houses.",
                     "status": "start"}
OTHER_QUAKE = QUAKE | {"title": "Earthquake strikes Sichuan", "summary": "An earthquake hit Luding county in Sichuan, killing 46 people.",
                       "place": "Luding", "actors": ["China Earthquake Networks Center"], "status": "start"}


@pytest.fixture
def quakes(database):
    """The quake today; one in Xizang a year ago, closed after it escalated; one in Sichuan half a year ago, still
    going; the rates a month ago; a quake in Xizang in ten days, beyond the time an event stays open."""
    def one(key, report, published_at, *later):
        candidate = reported(database, key, report, published_at=published_at)
        for index, (status, after) in enumerate(later):
            reported(database, f"{key}{index}", report | {"status": status}, published_at=published_at + after)
        return candidate

    candidates = {
        "today": one("a", QUAKE, NOW),
        "old": one("b", OLD_QUAKE, NOW - 365 * DAYS, ("escalation", 2 * HOURS), ("closure", 3 * DAYS), ("closure", 4 * DAYS)),
        "sichuan": one("c", OTHER_QUAKE, NOW - 180 * DAYS),
        "rates": one("d", RATES, NOW - 30 * DAYS),
        "later": one("e", OLD_QUAKE, NOW + 10 * DAYS),
    }
    assign_all(database, Ollama(status=503))
    found = assigned(database)
    events = {name: found[candidate].event_id for name, candidate in candidates.items()}
    named(database, candidates["today"], "authority", "China Earthquake Networks Center")
    with database.begin() as connection:
        entity_id = connection.execute(text("SELECT id FROM entity")).scalar_one()
        alias_id = connection.execute(select(EntityAlias.id).where(EntityAlias.entity_id == entity_id)).scalar_one()
        connection.execute(insert(EntityMention).values(candidate_id=candidates["sichuan"], alias_id=alias_id, source="actor", position=0))
    return events


def test_comparable_earlier_events_are_of_its_kind_with_why_and_how_they_went_on(database, quakes):
    comparison = event_context_service.earlier_events(database, quakes["today"])
    assert [each.event.id for each in comparison.events] == [quakes["old"], quakes["sichuan"]]
    old, sichuan = comparison.events
    assert old.similarity > sichuan.similarity >= event_context_service.MIN_SIMILARITY
    assert (old.shared_types, old.shared_countries, old.shared_entities) == (["Naturkatastrophe"], ["CN"], [])
    assert [entity.name for entity in sichuan.shared_entities] == ["China Earthquake Networks Center"]
    # Closed since its first closure, the repeated one changed nothing; it escalated before.
    assert (old.outcome.state, old.outcome.escalated) == ("closure", True)
    assert old.outcome.closed_at == NOW - 365 * DAYS + 3 * DAYS
    assert old.outcome.duration == 4 * DAYS
    assert (sichuan.outcome.state, sichuan.outcome.escalated, sichuan.outcome.closed_at) == ("start", False, None)
    assert (comparison.count("closed"), comparison.count("ongoing"), comparison.count("escalated")) == (1, 1, 1)
    assert old.uncertainty.reports == 4


def test_a_later_event_and_one_of_another_kind_are_not_earlier_comparable(database, quakes):
    # The quake in ten days sees today's and the older ones; the rates share no type and no entity.
    later = event_context_service.earlier_events(database, quakes["later"])
    assert quakes["today"] in [each.event.id for each in later.events]
    assert quakes["rates"] not in [each.event.id for each in later.events]
    assert event_context_service.earlier_events(database, quakes["old"]).events == []


def test_a_merged_event_has_no_comparable_earlier_events(database, quakes):
    event_correction_service.merge(database, quakes["sichuan"], [quakes["today"]])
    assert event_context_service.earlier_events(database, quakes["today"]).events == []
    with database.connect() as connection:
        assert connection.execute(select(Event.merged_into_id).where(Event.id == quakes["today"])).scalar_one() == quakes["sichuan"]


def test_an_old_period_ends_at_the_end_of_its_day(database, history):
    form = event_service.parse_filter({"from": (NOW - 365 * DAYS).date().isoformat(), "to": (NOW - 365 * DAYS).date().isoformat()}, now=NOW)
    assert form.criteria.until - form.criteria.since == DAYS
    assert {event.id for event in event_service.overview(database, form.criteria).events} == {history["quake"]}
