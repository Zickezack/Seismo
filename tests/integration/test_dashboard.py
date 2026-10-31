"""The start page in PostgreSQL: which events are new and which developing within a period, and their changes of state
(dashboard_service)."""
from datetime import timedelta

from app.services import dashboard_service, event_service
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_events import QUAKE, RATES, SAME_QUAKE, assign_all, assigned, reported
from tests.integration.test_extraction import RATE_DECISION, Ollama
from tests.integration.test_ingestion import NOW
from tests.integration.test_reprocessing import old_reading

STRIKE = {"title": "Dock workers strike in the port of Rotterdam", "place": "Rotterdam", "actors": ["FNV Havens"],
          "summary": "Dock workers stopped work in the port of Rotterdam over wages.", "event_type": "labor", "country": "NL"}
FLOOD = {"title": "Floods cut rail lines in Valencia", "place": "Valencia", "actors": ["Adif"],
         "summary": "Heavy rain flooded rail lines around Valencia, halting trains.", "event_type": "natural_disaster", "country": "ES"}
HOURS = timedelta(hours=1)


def test_new_events_and_developing_ones_with_a_change_of_state_first(database):
    [escalating] = reported(database, "a", QUAKE | {"status": "start"}, published_at=NOW - 48 * HOURS)
    reported(database, "b", SAME_QUAKE | {"status": "escalation"}, published_at=NOW - HOURS)
    [continuing] = reported(database, "c", STRIKE | {"status": "update"}, published_at=NOW - 30 * HOURS)
    reported(database, "d", STRIKE | {"status": "update", "title": "Dock workers in Rotterdam continue their strike"},
             published_at=NOW - 3 * HOURS)
    [new] = reported(database, "e", RATES | {"status": "start"}, published_at=NOW - 2 * HOURS)
    [old] = reported(database, "f", FLOOD | {"status": "start"}, published_at=NOW - 72 * HOURS)
    assign_all(database, Ollama(status=503))
    events = {name: assigned(database)[candidate].event_id for name, candidate in
              (("escalating", escalating), ("continuing", continuing), ("new", new), ("old", old))}

    board = dashboard_service.overview(database, "24h", now=NOW)

    assert [item.event.id for item in board.new] == [events["new"]]
    # The escalation within the period first; the continuation repeats its state - no change.
    assert [item.event.id for item in board.developing] == [events["escalating"], events["continuing"]]
    assert [change.status for change in board.developing[0].changes] == ["escalation"] and board.developing[1].changes == []
    assert (board.developing[0].recent_reports, board.developing[0].event.report_count) == (1, 2)
    assert (board.new_total, board.developing_total, board.changed_total) == (1, 2, 1)
    # Three days back, every one of them was first reported within the period.
    wider = dashboard_service.overview(database, "3d", now=NOW)
    assert {item.event.id for item in wider.new} == {events["new"], events["old"], events["escalating"], events["continuing"]}


def test_the_event_page_traces_each_statement_of_its_reports_to_its_quote(database):
    reading = old_reading(database)
    assign_all(database, Ollama(status=503))
    event_id = assigned(database)[reading.candidate_id].event_id

    detail = event_service.detail(database, event_id)

    [statement] = detail.statements[reading.candidate_id]
    assert (statement.kind_label, statement.quote) == ("Tatsache", RATE_DECISION)
    [source] = detail.sources
    assert (source.name, source.report_count) == ("Sa", 1)
