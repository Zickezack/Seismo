"""The development of an event (Ereignisidentität story 2): what a report does to the state in effect at its time.

Through real assignments, with the versions in PostgreSQL: tests/integration/test_events.py.
"""
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db.models import EventStatus
from app.services import event_service
from app.services.event_service import StateEffect, StateView, state_effect
from tests.unit.test_web_events import report

START, UPDATE, ESCALATION, CLOSURE, RESUMPTION, UNKNOWN = (status.value for status in EventStatus)
VERSION, CONFIRMS, CONTRADICTS, NONE = StateEffect.VERSION, StateEffect.CONFIRMS, StateEffect.CONTRADICTS, StateEffect.NONE
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
MIGRATION = Path(__file__).parents[2] / "migrations/versions/20261029_5d0c7e1a9b42_event_states.py"


@pytest.mark.parametrize("current, reported, effect", [
    # The earliest known status founds the state, whatever it is.
    *[(None, status, VERSION) for status in (START, UPDATE, ESCALATION, CLOSURE, RESUMPTION)],
    # Ongoing.
    (START, UPDATE, CONFIRMS), (START, ESCALATION, VERSION), (START, CLOSURE, VERSION),
    (START, START, CONFIRMS), (START, RESUMPTION, CONTRADICTS),
    (UPDATE, UPDATE, CONFIRMS), (UPDATE, START, CONTRADICTS), (UPDATE, RESUMPTION, CONTRADICTS),
    (UPDATE, ESCALATION, VERSION), (UPDATE, CLOSURE, VERSION),
    (ESCALATION, ESCALATION, CONFIRMS), (ESCALATION, UPDATE, CONFIRMS), (ESCALATION, CLOSURE, VERSION),
    (ESCALATION, START, CONTRADICTS),
    (RESUMPTION, RESUMPTION, CONFIRMS), (RESUMPTION, ESCALATION, VERSION), (RESUMPTION, START, CONTRADICTS),
    # Closed.
    (CLOSURE, CLOSURE, CONFIRMS), (CLOSURE, RESUMPTION, VERSION),
    (CLOSURE, START, CONTRADICTS), (CLOSURE, UPDATE, CONTRADICTS), (CLOSURE, ESCALATION, CONTRADICTS),
    # Unknown changes nothing.
    (None, UNKNOWN, NONE), (CLOSURE, UNKNOWN, NONE),
])
def test_what_a_report_does_to_the_state_in_effect(current, reported, effect):
    assert state_effect(current, reported) == effect


def test_the_migration_replayed_the_reports_with_the_same_rule():
    spec = importlib.util.spec_from_file_location("event_states_migration", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statuses = [status.value for status in EventStatus]

    for current in [None, *(status for status in statuses if status != UNKNOWN)]:
        for reported in statuses:
            assert migration._starts_version(current, reported) == (state_effect(current, reported) == VERSION)


def state(state_id, status, valid_from, candidate_id):
    return StateView(state_id, status, valid_from, valid_from, candidate_id, "Report", None, 1, "Xinhua")


def test_a_report_is_labelled_by_the_version_in_effect_at_its_own_time():
    states = [state(1, START, NOW, 10), state(2, CLOSURE, NOW + timedelta(hours=5), 13)]

    def labelled(candidate_id, status, hours):
        row = report(candidate_id, "score", status=status, moment=NOW + timedelta(hours=hours))
        return event_service._report(_Row(row), states)

    assert labelled(10, START, 0).effect_label == "Zustandswechsel"
    assert labelled(11, UPDATE, 1).effect_label == "bestätigt"
    assert labelled(12, START, 2).effect_label == "bestätigt"
    assert labelled(17, RESUMPTION, 2).effect_label == "widerspricht (Beginn)"
    assert labelled(13, CLOSURE, 5).effect_label == "Zustandswechsel"
    assert labelled(14, UPDATE, 6).effect_label == "widerspricht (Abschluss)"
    assert labelled(15, UNKNOWN, 6).effect_label == "–"
    # Before every version, yet no version of its own: a later report was assigned first. No guess.
    assert labelled(16, ESCALATION, -1).effect == NONE


class _Row:
    """A report view as the repository's row: its fields by name."""

    def __init__(self, view):
        self._mapping = {name: getattr(view, name) for name in view.__dataclass_fields__}
