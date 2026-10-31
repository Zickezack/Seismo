"""Reprocessing in PostgreSQL: runs made with an older schema version or another model run again - an article's new
extraction takes the place of its old reading in the events, a recognition run again replaces its mentions and its
Angaben - and the storage the data takes, measured once a day - against a simulated Ollama."""
import copy
import logging
import threading
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from sqlalchemy import delete, select, update

from app.db.models import (
    EntityMention,
    EntityRecognition,
    Event,
    EventCandidate,
    EventCorrection,
    Extraction,
    RawText,
    RelationshipRecord,
)
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.llm.limiter import LlmCallLimiter
from app.llm.ollama import OllamaClient
from app.repositories import extraction_repository
from app.services import (
    backup_service,
    entity_service,
    event_service,
    job_service,
    platform_settings_service,
    relationship_service,
    reprocessing_service,
    storage_service,
)
from app.services.reprocessing_service import InvalidReprocessing
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_entities import entity_named, item, recognise_all, recognition, rows
from tests.integration.test_events import assign_all, assigned
from tests.integration.test_events import reported as reported_events
from tests.integration.test_extraction import INVALID, RATE_DECISION, Ollama, add, answer, source
from tests.integration.test_ingestion import NOW, jobs
from tests.integration.test_normalization import normalize_all
from tests.integration.test_relationships import related
from tests.integration.test_relationships import reported as reported_with_statements

MODEL = "qwen3.5:9b"
EXTRACT, RECOGNISE = job_service.REEXTRACT_EVENTS_JOB_KIND, job_service.RERECOGNISE_ENTITIES_JOB_KIND
OLD_EXTRACTIONS = f"extraction|1|{MODEL}"
# The old reading of the article: the report answer() gives, made with schema 1, its statement citable.
OLD_REPORT = {
    "event_type": "monetary_policy", "status": "update", "title": "ECB keeps rates unchanged",
    "summary": "The ECB left its key rates unchanged.", "country": None, "place": "euro area",
    "actors": [{"name": "ECB", "role": "central bank", "quote": "Die Europäische Zentralbank"},
               {"name": "Deutsche Bank", "role": "bank", "quote": "Deutsche Bank"}],
    "impacts": [],
    "statements": [{"kind": "fact", "text": "The ECB regulates Deutsche Bank.", "quote": RATE_DECISION, "confidence": "high",
                    "source_quality": None}],
}


def old_reading(database, key="a", published_at=NOW):
    """An article with its text, extracted with schema 1 into OLD_REPORT."""
    source_id = source(database, f"S{key}")
    article_id = add(database, source_id, key, published_at=published_at)
    normalize_all(database)
    values = {"article_id": article_id, "model": MODEL, "schema_version": "1", "duration_ms": 1, "input_chars": 1, "truncated": False}
    with database.begin() as connection:
        extraction_id = extraction_repository.insert_extraction(connection, values, [copy.deepcopy(OLD_REPORT)])
        candidate_id = connection.execute(select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id)).scalar_one()
    return SimpleNamespace(source_id=source_id, article_id=article_id, extraction_id=extraction_id, candidate_id=candidate_id)


def run_jobs(database, ollama, *kinds):
    llm = partial(OllamaClient, "http://ollama:11434", MODEL, 8192, transport=httpx2.MockTransport(ollama))
    stop = threading.Event()
    handlers = build_handlers(database, Path("/nonexistent"), LlmCallLimiter(database, stop), stop, "ops@example.org", llm)
    handlers = {kind: handlers[kind] for kind in kinds}
    while run_next_job(database, handlers, "test", 10, on_renewed=lambda: None):
        pass


def extraction(database, extraction_id):
    with database.connect() as connection:
        return connection.execute(select(Extraction.__table__).where(Extraction.id == extraction_id)).one()


def current_extraction(database, article_id):
    with database.connect() as connection:
        return extraction_repository.latest(connection, article_id)


def reports_of(database, extraction_id):
    with database.connect() as connection:
        return list(connection.execute(
            select(EventCandidate.id).where(EventCandidate.extraction_id == extraction_id).order_by(EventCandidate.position)
        ).scalars())


def event(database, event_id):
    with database.connect() as connection:
        return connection.execute(select(Event.__table__).where(Event.id == event_id)).one()


def test_a_new_extraction_takes_the_place_of_the_old_reading_in_its_event(database, caplog):
    old = old_reading(database)
    # Another article's report in the same words: the same event, by the score alone.
    [other] = reported_events(database, "b", {
        "title": "ECB keeps rates unchanged", "summary": "The ECB left its key rates unchanged.", "place": "euro area",
        "actors": ["ECB", "Deutsche Bank"], "event_type": "monetary_policy", "country": None,
    }, published_at=NOW + timedelta(hours=1))
    assign_all(database, Ollama("{}"))
    event_id = assigned(database)[old.candidate_id].event_id
    assert assigned(database)[other].event_id == event_id and event(database, event_id).founding_candidate_id == old.candidate_id
    recognise_all(database, Ollama(recognition(
        item(1, "authority", "European Central Bank"), item(2, "company", "Deutsche Bank"), relationships=[related(1, "regulates", 2)],
    )))
    bank = entity_named(database, "Deutsche Bank").id
    assert [view.report_count for view in relationship_service.of_entity(database, bank)] == [1]

    reprocessing = reprocessing_service.start(database, {"group": OLD_EXTRACTIONS}, MODEL)
    assert reprocessing.total == 1
    assert len(reprocessing_service.enqueue_due(database)) == 1
    [job] = jobs(database, EXTRACT)
    assert (job.priority, job.payload) == (1, {"article_id": old.article_id, "extraction_id": old.extraction_id, "reprocessing_id": reprocessing.id})
    # A first answer outside the schema is stored as a run of the reprocessing, too.
    with caplog.at_level(logging.INFO):
        run_jobs(database, Ollama(INVALID, answer(("fact", RATE_DECISION))), EXTRACT)

    new = current_extraction(database, old.article_id)
    assert (new.schema_version, new.reprocessing_id) == ("2", reprocessing.id)
    assert extraction(database, old.extraction_id).replaced_by_id == new.id
    # The old reading's Angabe counts no more, from now on; it stays.
    [record] = rows(database, RelationshipRecord)
    assert record.replaced_at is not None and relationship_service.of_entity(database, bank) == []
    # Its report stays in the event until the new reading has its own: the new one finds it there.
    assert reprocessing_service.retire_replaced(database) == 0
    assert assigned(database)[old.candidate_id].event_id == event_id
    [new_report] = reports_of(database, new.id)
    assign_all(database, Ollama("{}"))
    assert (assigned(database)[new_report].event_id, assigned(database)[new_report].assignment_method) == (event_id, "score")

    assert reprocessing_service.retire_replaced(database) == 1

    assert assigned(database)[old.candidate_id].event_id is None
    # The event is named after the article's new report now.
    assert event(database, event_id).founding_candidate_id == new_report
    detail = event_service.detail(database, event_id)
    assert [report.id for report in detail.reports] == [new_report, other]
    [correction] = detail.corrections
    assert (correction.kind_label, correction.candidate_ids, correction.others) == ("Neuverarbeitung", [old.candidate_id], [])
    [stored] = rows(database, EventCorrection)
    assert stored.moves == [{"event_id": event_id, "candidate_ids": [old.candidate_id]}]
    # The version the old report stated is withdrawn by the correction, never deleted.
    old_versions = [state for state in detail.states if state.candidate_id == old.candidate_id]
    assert old_versions and all(state.withdrawn_by_id == correction.id for state in old_versions)
    # Never assigned again, never recognised.
    assert event_service.enqueue_due(database) == [] and reprocessing_service.retire_replaced(database) == 0
    assert entity_service.enqueue_due(database)  # the new reading's recognition
    # Every run it selected was made: done.
    assert reprocessing_service.enqueue_due(database) == []
    [view] = reprocessing_service.overview(database, MODEL).reprocessings
    assert (view.status_label, view.made, view.total, view.open_jobs) == ("Abgeschlossen", 1, 1, 0)
    # IDs only (datamodel.md, rule 13).
    assert "ECB keeps" not in caplog.text


def test_a_new_reading_without_reports_takes_the_old_ones_out_of_their_events_at_once(database):
    old = old_reading(database)
    assign_all(database, Ollama("{}"))
    event_id = assigned(database)[old.candidate_id].event_id
    recognise_all(database, Ollama(recognition(item(1, "authority", "European Central Bank"))))
    with database.begin() as connection:
        connection.execute(update(EntityRecognition).values(schema_version="1"))

    # The new extraction brings its own recognition: the old one's needs no job.
    assert reprocessing_service.reprocess_article(database, old.article_id, ["extraction", "recognition"], MODEL) == ["extraction"]
    [job] = jobs(database, EXTRACT)
    assert (job.priority, job.dedupe_key) == (0, f"{EXTRACT}:0:{old.extraction_id}")
    run_jobs(database, Ollama('{"events": []}'), EXTRACT)

    assert reprocessing_service.retire_replaced(database) == 1
    assert assigned(database)[old.candidate_id].event_id is None
    # An event left without reports is no longer listed; with none of the new reading to point to, it is merged nowhere.
    assert event(database, event_id).merged_into_id is None and event_service.listed(database, [event_id]) == []
    # The article is up to date now.
    with pytest.raises(InvalidReprocessing, match="nicht neu zu verarbeiten: Ereignisextraktion"):
        reprocessing_service.reprocess_article(database, old.article_id, ["extraction"], MODEL)


def test_an_event_left_without_reports_points_to_the_one_the_new_reading_went_to(database):
    old = old_reading(database)
    no_event = '{"reason": "Another incident.", "same_as": null}'
    assign_all(database, Ollama(no_event))
    event_id = assigned(database)[old.candidate_id].event_id
    # The new reading tells of another incident: its report founds an event of its own.
    other_incident = answer(("fact", RATE_DECISION)).replace("ECB keeps rates unchanged", "Dock workers strike in Rotterdam").replace(
        "The ECB left its key rates unchanged.", "Dock workers stopped work in the port of Rotterdam."
    )
    reprocessing_service.reprocess_article(database, old.article_id, ["extraction"], MODEL)
    run_jobs(database, Ollama(other_incident), EXTRACT)
    [new_report] = reports_of(database, current_extraction(database, old.article_id).id)
    assign_all(database, Ollama(no_event))
    new_event = assigned(database)[new_report].event_id
    assert new_event != event_id

    reprocessing_service.retire_replaced(database)

    assert event(database, event_id).merged_into_id == new_event
    assert event_service.detail(database, event_id).merged_into.id == new_event


def test_a_recognition_run_again_replaces_the_mentions_and_the_angaben_of_the_run_before(database):
    candidate_id = reported_with_statements(database, "a", ["TSMC", "Apple"], [("TSMC supplies Apple.", "TSMC supplies Apple")])
    recognise_all(database, Ollama(recognition(item(1, "company", "TSMC"), item(2, "company", "Apple"), relationships=[related(1, "supplies", 2)])))
    # As if recognised with schema 1.
    with database.begin() as connection:
        connection.execute(update(EntityRecognition).values(schema_version="1"))
    [group] = reprocessing_service.groups(database, MODEL)["recognition"]
    assert (group.schema_version, group.count, group.current) == ("1", 1, False)

    reprocessing = reprocessing_service.start(database, {"group": group.key}, MODEL)
    reprocessing_service.enqueue_due(database)
    run_jobs(database, Ollama(recognition(item(1, "company", "TSMC"), item(2, "none"))), RECOGNISE)

    tsmc = entity_named(database, "TSMC").id
    assert [(mention.candidate_id, mention.position) for mention in rows(database, EntityMention)] == [(candidate_id, 0)]
    [run] = rows(database, EntityRecognition)
    assert (run.schema_version, run.linked, run.relationships, run.reprocessing_id) == ("2", 1, 0, reprocessing.id)
    [record] = rows(database, RelationshipRecord)
    assert record.replaced_at is not None and relationship_service.of_entity(database, tsmc) == []
    [step] = relationship_service.detail(database, record.relationship_id).records
    assert step.replaced and not step.changed
    # Apple keeps its name forms, only no report names it any more.
    assert entity_service.detail(database, entity_named(database, "Apple").id).mention_total == 0
    assert reprocessing_service.enqueue_due(database) == []
    [view] = reprocessing_service.overview(database, MODEL).reprocessings
    assert (view.status_label, view.made) == ("Abgeschlossen", 1)


def test_a_recognition_of_a_reading_replaced_meanwhile_is_left_out(database):
    old = old_reading(database)
    reprocessing_service.reprocess_article(database, old.article_id, ["extraction"], MODEL)
    run_jobs(database, Ollama(answer(("fact", RATE_DECISION))), EXTRACT)
    # A recognition queued for the old reading before it was replaced.
    with database.begin() as connection:
        job_service.enqueue(connection, job_service.RECOGNISE_ENTITIES_JOB_KIND, {"extraction_id": old.extraction_id})

    recognise_all(database, Ollama(recognition(item(1, "authority", "European Central Bank"))))

    assert [run.extraction_id for run in rows(database, EntityRecognition)] == [current_extraction(database, old.article_id).id]


def test_a_reprocessing_takes_the_newest_first_within_its_budget_and_one_runs_at_a_time(database):
    first = old_reading(database, "a", published_at=NOW - timedelta(days=3))
    old_reading(database, "b", published_at=NOW - timedelta(days=1))
    third = old_reading(database, "c", published_at=NOW)
    # An article whose text is gone cannot be extracted again.
    with database.begin() as connection:
        connection.execute(delete(RawText).where(RawText.article_id == first.article_id))
    platform_settings_service.save(database, {"reprocessings_per_hour": "1"})

    def preview(**choice):
        found = reprocessing_service.overview(database, MODEL, {"group": OLD_EXTRACTIONS} | choice).preview
        return found.count, found.without_text

    assert preview() == (2, 1)
    assert preview(period_start=(NOW - timedelta(days=2)).date().isoformat()) == (2, 0)
    assert preview(period_end=(NOW - timedelta(days=2)).date().isoformat()) == (0, 1)
    assert preview(source_id=str(third.source_id)) == (1, 0)

    reprocessing = reprocessing_service.start(database, {"group": OLD_EXTRACTIONS}, MODEL)
    assert reprocessing.total == 2
    with pytest.raises(InvalidReprocessing, match="läuft bereits"):
        reprocessing_service.start(database, {"group": OLD_EXTRACTIONS}, MODEL)
    assert len(reprocessing_service.enqueue_due(database)) == 1
    assert [job.payload["article_id"] for job in jobs(database, EXTRACT)] == [third.article_id]
    # The hour's budget is spent, and something is left: it keeps running.
    assert reprocessing_service.enqueue_due(database) == []
    assert reprocessing_service.overview(database, MODEL).running.id == reprocessing.id

    assert reprocessing_service.cancel(database, [reprocessing.id]).processed == 1
    assert reprocessing_service.cancel(database, [reprocessing.id]).blocked == [f"Neuverarbeitung #{reprocessing.id} (Abgebrochen)"]
    platform_settings_service.save(database, {"reprocessings_per_hour": "10"})
    assert reprocessing_service.enqueue_due(database) == []


@pytest.mark.parametrize(("choice", "message"), [
    ({"group": "extraction|9|other"}, "Stand: bitte einen der aufgeführten wählen."),
    ({"group": OLD_EXTRACTIONS, "source_id": "x"}, "Quelle: bitte eine aus der Liste wählen."),
    ({"group": OLD_EXTRACTIONS, "source_id": "999999"}, "Quelle nicht gefunden."),
    ({"group": OLD_EXTRACTIONS, "period_start": "gestern"}, "Von: bitte ein Datum wählen."),
    ({"group": OLD_EXTRACTIONS, "period_start": "2026-10-20", "period_end": "2026-10-10"}, "Von: darf nicht nach Bis liegen."),
    ({"group": OLD_EXTRACTIONS, "period_end": "2000-01-01"}, "Die Auswahl umfasst nichts, was neu verarbeitet werden kann."),
])
def test_a_choice_that_selects_nothing_or_is_invalid_starts_nothing(database, choice, message):
    old_reading(database)

    with pytest.raises(InvalidReprocessing) as raised:
        reprocessing_service.start(database, choice, MODEL)

    assert str(raised.value) == message and reprocessing_service.overview(database, MODEL).reprocessings == []


def test_runs_made_as_now_are_current_and_offer_nothing(database):
    old = old_reading(database)

    [group] = reprocessing_service.groups(database, "other-model")["extraction"]
    assert not group.current
    [group] = reprocessing_service.groups(database, MODEL)["extraction"]
    assert not group.current and reprocessing_service.article_state(database, old.article_id, MODEL).extraction_outdated
    with database.begin() as connection:
        connection.execute(update(Extraction).values(schema_version="2"))

    [group] = reprocessing_service.groups(database, MODEL)["extraction"]
    assert group.current
    with pytest.raises(InvalidReprocessing, match="dieser ist aktuell"):
        reprocessing_service.start(database, {"group": group.key}, MODEL)
    assert reprocessing_service.article_state(database, old.article_id, MODEL).outdated == {}


def test_the_storage_is_measured_once_a_day_and_its_growth_follows_from_two(database, tmp_path):
    add(database, source(database), "a")
    backup = tmp_path / backup_service.backup_name(database.url.database, NOW)
    backup.write_bytes(b"x" * 1000)

    assert storage_service.measure_if_due(database, tmp_path, NOW)
    assert not storage_service.measure_if_due(database, tmp_path, NOW + timedelta(hours=5))
    assert storage_service.measure_if_due(database, tmp_path, NOW + timedelta(days=1))

    history = storage_service.history(database)
    assert [each.measured_on for each in history] == [NOW.date() + timedelta(days=1), NOW.date()]
    assert (history[0].raw_text_count, history[0].article_count, history[0].backup_bytes) == (1, 1, 1000)
    assert history[0].backup_free_bytes > 0
    growth = storage_service.growth(history, 7)
    assert (growth.period, growth.days, growth.articles) == (7, 1, 0)
    now = storage_service.current(database, tmp_path)
    assert (now.raw_text_count, now.backup_count) == (1, 1) and now.raw_text_bytes > 0
    assert [(each.name, each.count) for each in now.by_source] == [("EZB", 1)]
    assert "raw_text" in {table.name for table in now.tables}
