"""Corrections of the grouping in PostgreSQL (Ereignisidentität story 3): merges and splits of events assigned for
real, against a simulated Ollama, with their audit trail and the state histories they derive anew."""
import json
import logging
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.jobs.errors import JobDeferred
from app.services import event_correction_service, event_service
from app.services.bulk import BulkResult
from app.services.event_service import ASSIGN_FROM, NEW_BELOW
from tests.integration.alembic_helpers import downgrade, upgrade
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_events import (  # noqa: F401 - grey is a fixture
    QUAKE,
    RATES,
    RESCUE,
    SAME_QUAKE,
    assign_all,
    assigned,
    event,
    grey,
    phase_one,
    reported,
    versions,
)
from tests.integration.test_extraction import Ollama
from tests.integration.test_ingestion import NOW

# The same quake in words of its own: nothing alike to QUAKE, so the assignment founds an event of its own.
TREMOR = {"title": "Tibet tremor: death toll rises", "summary": "Rescuers in Shigatse report more dead after the tremor.",
          "place": "Shigatse", "actors": [], "event_type": "natural_disaster", "country": "CN"}
AWAY = Ollama(status=503)


def states(database, event_id):
    return [(state.status, state.candidate_id, state.added_by_id, state.withdrawn_by_id)
            for state in event_service.detail(database, event_id).states]


def test_a_merge_moves_the_reports_to_the_event_and_keeps_the_merged_one_with_its_id(database, caplog):
    [quake] = reported(database, "a", QUAKE | {"status": "start"}, published_at=NOW - timedelta(hours=3))
    [tremor] = reported(database, "b", TREMOR | {"status": "escalation"}, published_at=NOW - timedelta(hours=1))
    assign_all(database, AWAY)
    kept, merged = assigned(database)[quake].event_id, assigned(database)[tremor].event_id
    assert kept != merged

    with caplog.at_level(logging.INFO, logger=event_correction_service.__name__):
        result = event_correction_service.merge(database, kept, [merged], "  Same quake  ")

    assert result == BulkResult(processed=1, blocked=[])
    moved = assigned(database)[tremor]
    # How it was assigned first stays with the report.
    assert (moved.event_id, moved.assignment_method) == (kept, "new")
    reach = event(database, kept)
    assert (reach.first_reported_at, reach.last_reported_at) == (NOW - timedelta(hours=3), NOW - timedelta(hours=1))
    assert (reach.title, reach.founding_candidate_id, reach.merged_into_id) == (QUAKE["title"], quake, None)
    assert event(database, merged).merged_into_id == kept
    overview = event_service.overview(database)
    assert ([item.id for item in overview.events], overview.total) == ([kept], 1)

    detail = event_service.detail(database, kept)
    [correction] = detail.corrections
    assert (correction.kind, correction.incoming, correction.candidate_ids, correction.reason) == ("merge", True, [tremor], "Same quake")
    assert [(other.id, other.title) for other in correction.others] == [(merged, TREMOR["title"])]
    assert [report.method_label for report in detail.reports] == ["Neues Ereignis", "Zusammenführung"]
    # Weighed against the quake's start, the escalation is a version of the event now, appended by the merge.
    assert states(database, kept) == [("start", quake, None, None), ("escalation", tremor, correction.id, None)]
    assert detail.event.state == "escalation"

    gone = event_service.detail(database, merged)
    assert (gone.merged_into.id, gone.merged_into.title, gone.reports) == (kept, QUAKE["title"], [])
    assert [(each.id, each.incoming, [other.id for other in each.others]) for each in gone.corrections] == [(correction.id, False, [kept])]
    # Its history until the merge stays as it was.
    assert states(database, merged) == [("escalation", tremor, None, None)]
    [logged] = [record for record in caplog.records if record.getMessage() == "Events merged"]
    assert (logged.event_id, logged.merged, logged.reports, logged.correction_id) == (kept, [merged], 1, correction.id)


def test_a_merge_withdraws_a_version_the_reports_no_longer_make(database):
    # The tremor's start, reported and assigned first, founds an event; the quake's update another.
    [update] = reported(database, "a", QUAKE | {"status": "update"}, published_at=NOW - timedelta(hours=1))
    [start] = reported(database, "b", TREMOR | {"status": "start"}, published_at=NOW - timedelta(hours=3))
    assign_all(database, AWAY)
    kept, merged = assigned(database)[update].event_id, assigned(database)[start].event_id

    event_correction_service.merge(database, kept, [merged])

    detail = event_service.detail(database, kept)
    [correction] = detail.corrections
    assert correction.reason is None
    # Replayed in the order assigned, the start founds the state and the update only confirms it.
    assert states(database, kept) == [("start", start, correction.id, None), ("update", update, None, correction.id)]
    assert detail.event.state == "start"
    assert {report.id: report.effect_label for report in detail.reports} == {start: "Zustandswechsel", update: "bestätigt"}


@pytest.fixture
def five(database):
    """One event of five reports: start, escalation, a start that contradicts, closure, resumption."""
    hours = [NOW - timedelta(hours=back) for back in (4, 3, 2, 1, 0)]
    reports = [
        reported(database, key, (QUAKE if key == "a" else SAME_QUAKE) | {"status": status}, published_at=hour)[0]
        for key, status, hour in zip("abcde", ("start", "escalation", "start", "closure", "resumption"), hours, strict=True)
    ]
    assign_all(database, AWAY)
    assert {assigned(database)[report].event_id for report in reports} == {assigned(database)[reports[0]].event_id}
    return assigned(database)[reports[0]].event_id, reports, hours


def test_a_split_makes_a_new_event_named_after_its_earliest_report_and_derives_both_histories_anew(database, five, caplog):
    event_id, [start, worse, again, over, back], hours = five
    assert versions(database, event_id) == [("start", start), ("escalation", worse), ("closure", over), ("resumption", back)]

    with caplog.at_level(logging.INFO, logger=event_correction_service.__name__):
        result = event_correction_service.split(database, event_id, [back, over], "Another quake")

    assert (result.processed, result.blocked) == (2, []) and result.event_id not in (None, event_id)
    new = event(database, result.event_id)
    assert (new.title, new.event_type, new.country, new.founding_candidate_id) == (SAME_QUAKE["title"], "natural_disaster", "CN", over)
    assert (new.first_reported_at, new.last_reported_at) == (hours[3], hours[4])
    # The event's reach ends with its latest report left.
    assert (event(database, event_id).first_reported_at, event(database, event_id).last_reported_at) == (hours[0], hours[2])
    assert {assigned(database)[report].event_id for report in (over, back)} == {result.event_id}

    detail = event_service.detail(database, event_id)
    [correction] = detail.corrections
    assert (correction.kind, correction.incoming, correction.candidate_ids) == ("split", False, [over, back])
    assert ([other.id for other in correction.others], correction.reason) == ([result.event_id], "Another quake")
    # Without the closure, the event is still escalated: closure and resumption withdrawn, kept to be seen.
    assert states(database, event_id) == [
        ("start", start, None, None), ("escalation", worse, None, None),
        ("closure", over, None, correction.id), ("resumption", back, None, correction.id),
    ]
    assert (detail.event.state, detail.contradictions) == ("escalation", 1)
    assert {report.id: report.effect_label for report in detail.reports}[again] == "widerspricht (Eskalation)"

    split_off = event_service.detail(database, result.event_id)
    assert states(database, result.event_id) == [("closure", over, correction.id, None), ("resumption", back, correction.id, None)]
    assert [(report.id, report.method_label, report.founding) for report in split_off.reports] == [
        (over, "Aufteilung", True), (back, "Aufteilung", False)
    ]
    assert [(each.id, each.incoming, [other.id for other in each.others]) for each in split_off.corrections] == [
        (correction.id, True, [event_id])
    ]
    [logged] = [record for record in caplog.records if record.getMessage() == "Event split"]
    assert (logged.event_id, logged.new_event_id, logged.reports) == (event_id, result.event_id, 2)


def test_split_off_and_merged_back_the_reports_tell_the_latest_correction(database, five):
    event_id, [start, worse, _again, over, back], _hours = five
    split = event_correction_service.split(database, event_id, [over, back])

    event_correction_service.merge(database, event_id, [split.event_id])

    detail = event_service.detail(database, event_id)
    assert [each.kind for each in detail.corrections] == ["split", "merge"]
    assert {report.id: report.method_label for report in detail.reports}[over] == "Zusammenführung"
    # The history is the one the reports made at first: the withdrawn versions stay, made anew by the merge.
    merge_id = detail.corrections[1].id
    in_effect = [(status, candidate) for status, candidate, _added, withdrawn in states(database, event_id) if withdrawn is None]
    assert in_effect == [("start", start), ("escalation", worse), ("closure", over), ("resumption", back)]
    assert [added for _status, _candidate, added, withdrawn in states(database, event_id) if withdrawn is None][2:] == [merge_id, merge_id]
    assert detail.event.state == "resumption"


def test_the_report_an_event_is_named_after_never_leaves_it(database, five):
    event_id, [start, worse, *_rest], _hours = five

    result = event_correction_service.split(database, event_id, [worse, start])

    assert (result.processed, result.blocked) == (0, [f"{QUAKE['title']} (benennt das Ereignis)"])
    assert event_correction_service.split(database, event_id, [worse, 999999]).blocked == ["Bericht 999999 (nicht in diesem Ereignis)"]
    assert {row.event_id for row in assigned(database).values()} == {event_id}
    assert event_service.detail(database, event_id).corrections == []
    assert event_service.overview(database).total == 1


def test_an_event_merged_already_takes_part_in_no_correction(database):
    [quake] = reported(database, "a", QUAKE, published_at=NOW - timedelta(hours=2))
    [tremor] = reported(database, "b", TREMOR, published_at=NOW - timedelta(hours=1))
    [rates] = reported(database, "c", RATES)
    assign_all(database, AWAY)
    kept, merged, other = (assigned(database)[candidate].event_id for candidate in (quake, tremor, rates))
    event_correction_service.merge(database, kept, [merged])
    blocked = f"{TREMOR['title']} (bereits zusammengeführt)"

    assert event_correction_service.merge(database, kept, [other, merged]) == BulkResult(processed=0, blocked=[blocked])
    assert event_correction_service.merge(database, merged, [other]).blocked == [blocked]
    assert event_correction_service.merge(database, kept, [999999]).blocked == ["Ereignis 999999 (nicht gefunden)"]
    assert event_correction_service.split(database, merged, [tremor]).blocked == [blocked]
    # Nothing merged nor selected: nothing done.
    assert event_correction_service.merge(database, kept, [kept]) == BulkResult(processed=0, blocked=[])
    assert assigned(database)[rates].event_id == other and event(database, other).merged_into_id is None
    assert len(event_service.detail(database, kept).corrections) == 1


def test_similar_events_are_the_ones_within_reach_that_share_words_the_most_similar_first(database):
    [quake] = reported(database, "a", QUAKE)
    reported(database, "b", RATES, published_at=NOW + timedelta(hours=1))
    assign_all(database, AWAY)
    # In the quake's grey zone, judged another incident by the LLM.
    [rescue] = reported(database, "c", RESCUE, published_at=NOW + timedelta(hours=2))
    assign_all(database, Ollama(json.dumps({"reason": "Another incident.", "same_as": None})))
    # The same quake, but eight days later: beyond the reach of its reports.
    reported(database, "d", SAME_QUAKE, published_at=NOW + timedelta(days=8))
    assign_all(database, AWAY)
    quake_event, rescue_event = assigned(database)[quake].event_id, assigned(database)[rescue].event_id
    assert len(event_service.overview(database).events) == 4

    found = event_correction_service.similar_events(database, quake_event)

    # The rate decision shares no word with it.
    assert [(each.event.id, each.event.report_count) for each in found] == [(rescue_event, 1)]
    assert NEW_BELOW <= found[0].score < ASSIGN_FROM
    event_correction_service.merge(database, quake_event, [rescue_event])
    assert event_correction_service.similar_events(database, quake_event) == []
    assert event_correction_service.similar_events(database, rescue_event) == []


def test_an_llm_decision_for_an_event_merged_meanwhile_is_made_again(database, grey):
    quake, rescue = grey
    effect = phase_one(database, rescue, Ollama(json.dumps({"reason": "Rescue work after that quake.", "same_as": 1})))
    quake_event = assigned(database)[quake].event_id
    [rates_event] = {row.event_id for row in assigned(database).values()} - {quake_event, None}
    # Meanwhile the analyst merges the quake's event into another: the event the LLM chose has no reports left.
    event_correction_service.merge(database, rates_event, [quake_event])

    with database.begin() as connection, pytest.raises(JobDeferred):
        effect(connection)

    assert assigned(database)[rescue].event_id is None


def test_the_migration_names_every_event_after_its_founding_report_and_its_downgrade_drops_withdrawn_versions(database, five):
    event_id, [start, worse, _again, over, back], _hours = five
    new_id = event_correction_service.split(database, event_id, [over, back]).event_id

    downgrade(database, "5d0c7e1a9b42")

    with database.connect() as connection:
        left = connection.execute(text("SELECT status, candidate_id FROM event_state WHERE event_id = :id ORDER BY valid_from"), {"id": event_id}).all()
    assert [tuple(row) for row in left] == [("start", start), ("escalation", worse)]
    upgrade(database)
    # By the report with its title: the founding one, and the earliest split off for the new event.
    assert (event(database, event_id).founding_candidate_id, event(database, new_id).founding_candidate_id) == (start, over)
    assert versions(database, new_id) == [("closure", over), ("resumption", back)]
