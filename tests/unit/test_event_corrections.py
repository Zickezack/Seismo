"""Corrections of the grouping (Ereignisidentität story 3) without a database: the history a replay derives, the
similarity of events, the reason.

Merges and splits in PostgreSQL: tests/integration/test_event_corrections.py.
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import EventStatus
from app.services import event_service
from app.services.event_correction_service import MAX_REASON_CHARS, InvalidReason, Version, _reason, replay
from app.services.event_service import CorrectionView, matches, related
from tests.unit.test_web_events import report

START, UPDATE, ESCALATION, CLOSURE, RESUMPTION, UNKNOWN = (status.value for status in EventStatus)
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Report:
    id: int
    status: str
    hours: float

    @property
    def moment(self) -> datetime:
        return NOW + timedelta(hours=self.hours)


def made(*reports) -> list[tuple[str, int]]:
    return [(version.status, version.candidate_id) for version in replay(reports)]


def test_a_replay_weighs_each_report_against_the_version_in_effect_at_its_own_time():
    reports = [Report(1, START, 0), Report(2, ESCALATION, 1), Report(3, START, 2), Report(4, CLOSURE, 3),
               Report(5, UPDATE, 4), Report(6, RESUMPTION, 5), Report(7, UNKNOWN, 6)]

    assert made(*reports) == [(START, 1), (ESCALATION, 2), (CLOSURE, 4), (RESUMPTION, 6)]
    assert replay(reports)[0] == Version(START, NOW, 1)


def test_a_replay_follows_the_order_of_assignment_not_of_time():
    # An update assigned first founds the state; a start reported earlier but assigned later is a version before it.
    assert made(Report(1, UPDATE, 1), Report(2, START, 0)) == [(START, 2), (UPDATE, 1)]
    # Assigned the other way round, the update only confirms the start.
    assert made(Report(2, START, 0), Report(1, UPDATE, 1)) == [(START, 2)]


def test_of_two_versions_at_the_same_time_the_one_made_last_is_in_effect():
    # The resumption, made last at hour 1, is in effect from there: the closure at hour 2 is a new version, where it
    # would only confirm the closure made first at hour 1.
    reports =[Report(1, START, 0), Report(2, CLOSURE, 1), Report(3, RESUMPTION, 1), Report(4, CLOSURE, 2)]

    assert made(*reports) == [(START, 1), (CLOSURE, 2), (RESUMPTION, 3), (CLOSURE, 4)]
    assert [version.valid_from for version in replay(reports)] == [NOW, *[NOW + timedelta(hours=hours) for hours in (1, 1, 2)]]


def test_nothing_known_makes_no_version():
    assert made(Report(1, UNKNOWN, 0)) == [] and made() == []


@dataclass(frozen=True)
class Text:
    event_id: int
    title: str
    summary: str = ""
    place: str | None = None
    actors: list = field(default_factory=list)
    article_id: int = 0


QUAKE = Text(1, "Earthquake strikes Xizang", "A strong earthquake hit Dingri county in Xizang.", "Dingri")
RESCUE = Text(1, "Rescue continues in Dingri", "Rescuers search the rubble after the earthquake.")
SAME = Text(2, "Earthquake strikes Xizang, killing 126", "An earthquake hit Dingri county; 126 people were killed.", "Dingri")
RATES = Text(3, "ECB keeps key interest rates unchanged", "The European Central Bank left its rates unchanged.", "Frankfurt")


def test_events_are_related_by_their_report_most_similar_to_any_report_of_the_event():
    found = related([QUAKE, RESCUE], [SAME, RATES, Text(4, "Rubble cleared in Dingri", "Rescuers left.")])

    # The rate decision shares no word: left out.
    assert [match.event_id for match in found] == [2, 4]
    assert found[0].score > found[1].score > 0


def test_one_report_is_related_to_events_as_an_assignment_scores_them():
    others = [SAME, RATES, Text(4, "Rubble cleared in Dingri", "Rescuers left.", article_id=5)]
    candidate = Text(1, QUAKE.title, QUAKE.summary, QUAKE.place, article_id=9)

    expected = [(match.event_id, round(match.score, 9)) for match in matches(candidate, others) if match.score > 0]
    assert [(match.event_id, round(match.score, 9)) for match in related([candidate], others)] == expected


def test_a_reason_is_kept_trimmed_and_an_empty_one_is_none():
    assert _reason("  Same quake  ") == "Same quake"
    assert _reason("   ") is None and _reason(None) is None
    assert _reason("x" * MAX_REASON_CHARS) == "x" * MAX_REASON_CHARS
    with pytest.raises(InvalidReason, match="1001 Zeichen, höchstens 1000"):
        _reason("x" * (MAX_REASON_CHARS + 1))


@pytest.mark.parametrize("kind, incoming, label", [
    ("merge", True, "Aufgenommen:"), ("merge", False, "Aufgegangen in"),
    ("split", True, "Abgespalten aus"), ("split", False, "Abgespalten als"),
])
def test_a_correction_reads_from_the_side_of_the_event_it_is_shown_on(kind, incoming, label):
    assert CorrectionView(1, kind, NOW, None, incoming, [], []).relation_label == label


def test_a_report_a_correction_brought_says_so_and_keeps_how_it_was_assigned_first():
    moved = report(2, "llm", 0.27, moved_by="split")

    assert (moved.method_label, moved.assignment_label) == ("Aufteilung", "LLM (Ähnlichkeit 0,27)")
    assert report(3, "score", 0.61).method_label == "Ähnlichkeit 0,61"
    assert event_service.CORRECTION_LABELS["merge"] == "Zusammenführung"
