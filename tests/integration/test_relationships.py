"""Relationships between entities in PostgreSQL: recorded from the reports' statements by the recognition, entered and
reviewed by the analyst, and carried along when entities are merged or name forms split off - against a simulated
Ollama."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import EntityAlias, EntityRecognition, EntityRelationship, EventCandidate, RelationshipRecord
from app.repositories import extraction_repository
from app.services import entity_correction_service, relationship_service, seed_service
from app.services.relationship_service import InvalidRelationship
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_entities import entity_named, item, question, recognise_all, recognition, rows
from tests.integration.test_extraction import Ollama, source
from tests.integration.test_ingestion import NOW
from tests.integration.test_normalization import article

TODAY = NOW.date()


def reported(database, key, actors, statements, published_at=NOW):
    """An article whose extraction found one report with these actors and statements (text, quote); its candidate."""
    article_id = article(database, source(database, f"R{key}"), key, url=f"https://news.example/{key}.html", published_at=published_at)
    report = {
        "event_type": "corporate", "status": "update", "title": f"Report {key}", "summary": "Summary.", "country": None, "place": None,
        "actors": [{"name": name, "role": "", "quote": name} for name in actors], "impacts": [],
        "statements": [{"kind": "fact", "text": text, "quote": quote, "confidence": "high", "source_quality": "medium"} for text, quote in statements],
    }
    values = {"article_id": article_id, "model": "m", "schema_version": "2", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        extraction_id = extraction_repository.insert_extraction(connection, values, [report])
        return connection.execute(select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id)).scalar_one()


def related(subject, kind, object_, statement=1, ended=False):
    return {"subject": subject, "kind": kind, "object": object_, "ended": ended, "statement": statement}


def relationship_of(database, kind):
    [row] = rows(database, EntityRelationship, EntityRelationship.kind == kind)
    return row


def state(database, relationship_id):
    detail = relationship_service.detail(database, relationship_id)
    return detail.state.valid_from, detail.state.valid_to, detail.state.review


def test_a_relationship_a_statement_states_is_recorded_with_its_evidence(database):
    candidate_id = reported(database, "a", ["TSMC", "Apple", "Residents"], [("TSMC supplies chips to Apple.", "TSMC liefert Chips an Apple")])
    ollama = Ollama(recognition(
        item(1, "company", "TSMC"), item(2, "company", "Apple"), item(3, "none"),
        relationships=[related(1, "supplies", 2), related(1, "regulates", 2), related(3, "supplies", 2)],
    ))

    recognise_all(database, ollama)

    assert 'Statements:\n1. TSMC supplies chips to Apple. Quote: "TSMC liefert Chips an Apple"' in question(ollama)
    supplies = relationship_of(database, "supplies")
    assert (supplies.subject_id, supplies.object_id) == (entity_named(database, "TSMC").id, entity_named(database, "Apple").id)
    assert rows(database, EntityRelationship, EntityRelationship.kind == "regulates") == []  # a company regulates nobody
    [record] = rows(database, RelationshipRecord)
    assert (record.origin, record.candidate_id, record.reported_on, record.ended) == ("report", candidate_id, TODAY, False)
    [run] = rows(database, EntityRecognition)
    assert (run.schema_version, run.relationships) == ("2", 1)
    detail = relationship_service.detail(database, supplies.id)
    assert (detail.state.valid_from, detail.state.review_label) == (TODAY, "Ungeprüft")
    assert (detail.records[0].record.quote, detail.records[0].source_quality_label) == ("TSMC liefert Chips an Apple", "Quelle mittel")
    [view] = relationship_service.of_entity(database, supplies.object_id)
    assert (view.role_label, view.other.name, view.report_count) == ("Lieferant", "TSMC", 1)


def test_a_later_report_of_its_end_closes_it_and_an_earlier_one_moves_its_start(database):
    reported(database, "a", ["TSMC", "Apple"], [("TSMC supplies Apple.", "TSMC supplies Apple")])
    recognise_all(database, Ollama(recognition(item(1, "company", "TSMC"), item(2, "company", "Apple"), relationships=[related(1, "supplies", 2)])))
    reported(database, "b", ["Apple", "TSMC"], [("Apple dropped TSMC.", "Apple dropped TSMC")], published_at=NOW + timedelta(days=5))
    reported(database, "c", ["TSMC", "Apple"], [("TSMC has long supplied Apple.", "TSMC has long supplied Apple")], published_at=NOW - timedelta(days=30))

    # The earliest article first: c, then b.
    recognise_all(database, Ollama(
        recognition(item(1, "company", "TSMC"), item(2, "company", "Apple"), relationships=[related(1, "supplies", 2)]),
        recognition(item(1, "company", "Apple"), item(2, "company", "TSMC"), relationships=[related(2, "supplies", 1, ended=True)]),
    ))

    supplies = relationship_of(database, "supplies")
    assert state(database, supplies.id) == (TODAY - timedelta(days=30), TODAY + timedelta(days=5), "unreviewed")
    assert len(rows(database, RelationshipRecord)) == 3


def test_a_competition_is_one_relationship_whichever_way_reports_name_it(database):
    reported(database, "a", ["Airbus", "Boeing"], [("Airbus competes with Boeing.", "Airbus competes with Boeing")])
    recognise_all(database, Ollama(recognition(item(1, "company", "Airbus"), item(2, "company", "Boeing"), relationships=[related(1, "competes", 2)])))
    reported(database, "b", ["Boeing", "Airbus"], [("Boeing's rival Airbus.", "Boeing's rival Airbus")])

    recognise_all(database, Ollama(recognition(item(1, "company", "Boeing"), item(2, "company", "Airbus"), relationships=[related(1, "competes", 2)])))

    competes = relationship_of(database, "competes")
    assert competes.subject_id < competes.object_id
    records = rows(database, RelationshipRecord)
    assert len(records) == 2 and {record.relationship_id for record in records} == {competes.id}
    airbus = rows(database, EntityAlias, EntityAlias.value == "Airbus")[0]
    assert {record.subject_alias_id if competes.subject_id == airbus.entity_id else record.object_alias_id for record in records} == {airbus.id}


def two_companies(database):
    reported(database, "a", ["Siemens", "Alstom"], [])
    recognise_all(database, Ollama(recognition(item(1, "company", "Siemens"), item(2, "company", "Alstom"))))
    return entity_named(database, "Siemens"), entity_named(database, "Alstom")


def test_the_analyst_records_a_relationship_with_its_source_confirmed(database):
    siemens, alstom = two_companies(database)

    relationship_id = relationship_service.add(database, siemens.id, "supplier", f"Alstom (Nr. {alstom.id})", "2024-01-01", "", "https://example.org/contract")

    supplies = relationship_of(database, "supplies")
    assert (supplies.id, supplies.subject_id, supplies.object_id) == (relationship_id, alstom.id, siemens.id)
    assert state(database, relationship_id) == (date(2024, 1, 1), None, "confirmed")
    [view] = relationship_service.of_entity(database, alstom.id)
    assert view.role_label == "Kunde"


@pytest.mark.parametrize(("role", "other", "valid_from", "valid_to", "note", "message"), [
    ("supplier", "Alstom", "", "", "", "Bitte eine Quelle angeben"),
    ("", "Alstom", "", "", "Quelle", "Bitte eine Rolle wählen"),
    ("supplier", "Bombardier", "", "", "Quelle", "Entität nicht gefunden"),
    ("supplier", "Siemens", "", "", "Quelle", "zwei verschiedene Entitäten"),
    ("regulator", "Alstom", "", "", "Quelle", "Unternehmen reguliert Unternehmen"),
    ("supplier", "Alstom", "2025-01-01", "2024-01-01", "Quelle", "Das Ende liegt vor dem Beginn"),
    ("supplier", "Alstom", "gestern", "", "Quelle", "Ein Datum ist ungültig"),
])
def test_a_relationship_that_cannot_be_is_not_recorded(database, role, other, valid_from, valid_to, note, message):
    siemens, _ = two_companies(database)

    with pytest.raises(InvalidRelationship, match=message):
        relationship_service.add(database, siemens.id, role, other, valid_from, valid_to, note)

    assert rows(database, RelationshipRecord) == []


def test_the_analyst_confirms_or_disputes_keeping_the_dates_and_sets_a_state(database):
    reported(database, "a", ["TSMC", "Apple"], [("TSMC supplies Apple.", "TSMC supplies Apple")])
    recognise_all(database, Ollama(recognition(item(1, "company", "TSMC"), item(2, "company", "Apple"), relationships=[related(1, "supplies", 2)])))
    tsmc, supplies = entity_named(database, "TSMC"), relationship_of(database, "supplies")

    confirmed = relationship_service.review(database, tsmc.id, [supplies.id], "confirmed")
    assert confirmed.processed == 1 and state(database, supplies.id) == (TODAY, None, "confirmed")
    relationship_service.review(database, tsmc.id, [supplies.id], "disputed", "Falsch übersetzt")
    assert state(database, supplies.id) == (TODAY, None, "disputed")
    relationship_service.set_state(database, supplies.id, "2020-05-01", "2025-12-31", "confirmed", "Geschäftsbericht 2025")
    assert state(database, supplies.id) == (date(2020, 5, 1), date(2025, 12, 31), "confirmed")

    # Every Angabe stays, in the order they were made.
    records = rows(database, RelationshipRecord)
    assert [(record.origin, record.review, record.note) for record in records] == [
        ("report", None, None), ("analyst", "confirmed", None), ("analyst", "disputed", "Falsch übersetzt"),
        ("analyst", "confirmed", "Geschäftsbericht 2025"),
    ]
    assert relationship_service.review(database, tsmc.id + 99, [supplies.id], "confirmed").blocked == [
        f"Beziehung {supplies.id} (nicht bei dieser Entität)"
    ]


def test_a_merge_carries_the_relationships_to_the_entity_merged_into(database):
    statements = [("TSMC supplies Apple Computer.", "TSMC supplies Apple Computer"), ("Apple and Apple Computer compete.", "Apple and Apple Computer compete")]
    reported(database, "a", ["TSMC", "Apple", "Apple Computer"], statements)
    recognise_all(database, Ollama(recognition(
        item(1, "company", "TSMC"), item(2, "company", "Apple"), item(3, "company", "Apple Computer"),
        relationships=[related(1, "supplies", 3), related(2, "competes", 3, statement=2)],
    )))
    apple, apple_inc = entity_named(database, "Apple"), entity_named(database, "Apple Computer")

    entity_correction_service.merge(database, apple.id, [apple_inc.id])

    [view] = [each for each in relationship_service.of_entity(database, apple.id) if each.kind == "supplies"]
    assert (view.role_label, view.other.name, view.report_count) == ("Lieferant", "TSMC", 1)
    # No entity competes with itself: that one stays with the entity merged away.
    assert [each.kind for each in relationship_service.of_entity(database, apple_inc.id)] == ["competes"]


def test_a_split_takes_along_the_reports_angaben_that_name_the_entity_by_the_form_split_off(database):
    reported(database, "a", ["Bank of Japan", "Toyota"], [("The BoJ oversees Toyota's bank.", "The BoJ oversees Toyota")])
    recognise_all(database, Ollama(recognition(item(1, "authority", "Bank of Japan"), item(2, "company", "Toyota"))))
    reported(database, "b", ["Bank of Korea", "Hyundai"], [("The Bank of Korea fined Hyundai.", "The Bank of Korea fined Hyundai")])
    recognise_all(database, Ollama(recognition(
        item(1, "authority", "Bank of Korea", same_as=1), item(2, "company", "Hyundai"), relationships=[related(1, "regulates", 2)],
    )))
    japan = entity_named(database, "Bank of Japan")
    korea_form = rows(database, EntityAlias, EntityAlias.value == "Bank of Korea")[0]
    assert korea_form.entity_id == japan.id and korea_form.uncertain
    hyundai = entity_named(database, "Hyundai")
    relationship_service.add(database, japan.id, "regulated", str(hyundai.id), "", "", "Notiz des Analysten")

    result = entity_correction_service.split(database, japan.id, [korea_form.id])

    korea = result.entity_id
    assert [(each.other.name, each.report_count) for each in relationship_service.of_entity(database, korea)] == [("Hyundai", 1)]
    # The analyst's Angabe was made for the Bank of Japan: it stays there.
    [kept] = relationship_service.of_entity(database, japan.id)
    assert (kept.other.name, kept.report_count, kept.state.review) == ("Hyundai", 0, "confirmed")


def test_a_relationship_needs_a_listed_commodity_to_depend_on(database):
    seed_service.import_entities(database)
    reported(database, "a", ["Japan", "Liquefied natural gas"], [("Japan depends on LNG imports.", "Japan depends on LNG imports")])

    recognise_all(database, Ollama(recognition(
        item(1, "country", "Japan", country_code="JP"), item(2, "commodity", "LNG", category="natural-gas"),
        relationships=[related(1, "depends_on", 2)],
    )))

    depends = relationship_of(database, "depends_on")
    assert (depends.subject_id, depends.object_id) == (entity_named(database, "Japan").id, entity_named(database, "Erdgas").id)
