"""The rules of putting events into context, without a database (userstories.md, Dashboard, Recherche und Alerts,
story 4): uncertainty counted and never combined, contradictions kept apart, the outcome of an earlier event, and the
historical search and period of the event filter."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import event_service
from app.services.event_context_service import (
    Comparison,
    EarlierEvent,
    Levels,
    Uncertainty,
    contradicting,
    duration_label,
    impact_conflicts,
    levels_by_event,
    outcome,
)
from app.services.event_service import EventListItem

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
HOUR = timedelta(hours=1)


def report(report_id, *impacts, status="update", moment=NOW, title=None):
    return SimpleNamespace(id=report_id, title=title or f"Report {report_id}", status=status, moment=moment,
                           impacts=[{"target": target, "direction": direction} for target, direction in impacts])


def version(status, valid_from, candidate_id):
    return SimpleNamespace(status=status, valid_from=valid_from, candidate_id=candidate_id)


def test_levels_count_each_level_and_unknown_never_combined():
    levels = Levels.of([("high", 3), ("low", 1), (None, 2), ("medium", 0), ("high", 1)])
    assert levels.counts == (4, 0, 1, 2)
    assert (levels.total, levels.weak) == (7, 3)
    assert levels.label == "4 hoch · 1 niedrig · 2 unbekannt"
    assert Levels().label == "–"


def test_the_statements_are_counted_by_event_kind_and_levels():
    rows = [
        SimpleNamespace(event_id=1, kind="fact", extraction_confidence="high", source_quality="medium", count=2),
        SimpleNamespace(event_id=1, kind="claim", extraction_confidence=None, source_quality="medium", count=1),
        SimpleNamespace(event_id=2, kind="fact", extraction_confidence="low", source_quality=None, count=1),
    ]
    found = levels_by_event(rows)
    kinds, extraction, quality = found[1]
    assert kinds == {"fact": 2, "claim": 1, "source_forecast": 0, "interpretation": 0}
    assert (extraction.counts, quality.counts) == ((2, 0, 0, 1), (0, 3, 0, 0))
    assert found[2][1].counts == (0, 0, 1, 0)


@pytest.mark.parametrize(("kinds", "label"), [
    ({"fact": 1, "claim": 2, "source_forecast": 0, "interpretation": 1}, "1 Tatsache, 2 Behauptungen, 1 Interpretation"),
    ({"source_forecast": 3}, "3 Quellenprognosen"),
    ({}, "keine Aussagen"),
])
def test_the_kinds_label_names_each_kind_with_its_number(kinds, label):
    assert Uncertainty(kinds=kinds).kinds_label == label


def test_opposite_directions_on_one_target_are_a_conflict_by_name_in_any_case():
    reports = [report(1, ("Maersk", "negative"), ("Oil", "mixed")), report(2, (" maersk ", "positive"), ("Oil", "unclear")),
               report(3, ("Maersk", "negative"), ("Gold", "positive"))]
    [conflict] = impact_conflicts(reports, {})
    assert conflict.target == "Maersk" and conflict.entity_id is None
    assert ([each.id for each in conflict.positive], [each.id for each in conflict.negative]) == ([2], [1, 3])


def test_one_report_seeing_a_target_both_ways_contradicts_no_other():
    alone = [report(1, ("Salvador transport", "negative"), ("Salvador transport", "positive")), report(2, ("Salvador transport", "mixed"))]
    assert impact_conflicts(alone, {}) == []
    # Another report on one side makes it a conflict; the report seeing both ways stays on both.
    [conflict] = impact_conflicts([*alone, report(3, ("Salvador transport", "negative"))], {})
    assert ([each.id for each in conflict.positive], [each.id for each in conflict.negative]) == ([1], [1, 3])


def test_an_impact_naming_an_entity_is_that_entity_whatever_its_name():
    reports = [report(1, ("Adif", "negative")), report(2, ("Spanish rail operator", "positive"))]
    targets = {(1, 0): (9, "Adif"), (2, 0): (9, "Adif")}
    [conflict] = impact_conflicts(reports, targets)
    assert (conflict.target, conflict.entity_id) == ("Adif", 9)
    # Without the entities the two names are two targets, each seen one way.
    assert impact_conflicts(reports, {}) == []


def test_a_report_contradicting_the_state_at_its_time_is_found_by_the_rule_of_the_assignment():
    versions = [version("start", NOW - 3 * HOUR, 1), version("closure", NOW - 2 * HOUR, 2)]
    reports = [report(1, status="start", moment=NOW - 3 * HOUR), report(2, status="closure", moment=NOW - 2 * HOUR),
               report(3, status="escalation", moment=NOW - HOUR), report(4, status="update", moment=NOW - 150 * timedelta(minutes=1)),
               report(5, status="unknown", moment=NOW)]
    # The escalation after the closure contradicts it; the update before it confirms the start; unknown is nothing.
    assert [each.id for each in contradicting(reports, versions)] == [3]


def test_a_repeated_version_changed_nothing_and_its_report_is_judged_against_the_state():
    versions = [version("start", NOW - 3 * HOUR, 1), version("start", NOW - HOUR, 2)]
    assert event_service.report_effect(2, "start", NOW - HOUR, versions) == (event_service.StateEffect.CONFIRMS, None)
    assert event_service.report_effect(1, "start", NOW - 3 * HOUR, versions) == (event_service.StateEffect.VERSION, None)


def item(state, first=NOW - 30 * HOUR, last=NOW):
    return EventListItem(id=1, title="Quake", event_type="natural_disaster", country="CN", first_reported_at=first,
                         last_reported_at=last, state=state, report_count=3, source_count=2)


def test_the_outcome_of_a_closed_event_counts_from_its_first_closure_in_a_row():
    versions = [version("start", NOW - 30 * HOUR, 1), version("escalation", NOW - 20 * HOUR, 2),
                version("closure", NOW - 10 * HOUR, 3), version("closure", NOW - 5 * HOUR, 4)]
    found = outcome(item("closure"), versions)
    assert (found.state, found.escalated, found.closed_at, found.duration) == ("closure", True, NOW - 10 * HOUR, 30 * HOUR)
    assert found.label == "Abschluss, eskaliert"


def test_a_resumed_event_is_no_longer_closed():
    versions = [version("start", NOW - 30 * HOUR, 1), version("closure", NOW - 10 * HOUR, 3), version("resumption", NOW - HOUR, 4)]
    found = outcome(item("resumption"), versions)
    assert (found.closed_at, found.escalated) == (None, False)
    assert outcome(item(None), []).label == "Zustand unbekannt"


@pytest.mark.parametrize(("duration", "label"), [
    (timedelta(minutes=20), "unter einer Stunde"), (HOUR, "1 Stunde"), (30 * HOUR, "30 Stunden"), (timedelta(days=9, hours=5), "9 Tage"),
])
def test_the_duration_label(duration, label):
    assert duration_label(duration) == label


def test_the_comparison_counts_closed_ongoing_unknown_and_escalated():
    def earlier(state, escalated):
        return EarlierEvent(item(state), 0.3, [], [], [], outcome(item(state), [version("escalation", NOW, 1)] if escalated else []), Uncertainty())

    comparison = Comparison([earlier("closure", True), earlier("update", True), earlier("start", False), earlier(None, False)])
    assert [comparison.count(kind) for kind in ("closed", "ongoing", "unknown", "escalated")] == [1, 2, 1, 2]


# The historical search and period of the event list.

def test_the_search_and_a_period_of_whole_utc_days_are_parsed_and_shown_again():
    form = event_service.parse_filter({"q": "  port   strike ", "from": "2025-11-03", "to": "2025-11-05"}, now=NOW)
    assert form.values == {"q": "port strike", "from": "2025-11-03", "to": "2025-11-05"}
    assert form.criteria.text == "port strike"
    assert form.criteria.since == datetime(2025, 11, 3, tzinfo=UTC)
    assert form.criteria.until == datetime(2025, 11, 6, tzinfo=UTC)
    assert form.active


@pytest.mark.parametrize("raw", ["03.11.2025", "2025-13-01", "yesterday", ""])
def test_a_day_not_in_the_form_of_the_date_field_narrows_nothing(raw):
    form = event_service.parse_filter({"from": raw, "to": raw}, now=NOW)
    assert (form.criteria.since, form.criteria.until) == (None, None)
    assert "from" not in form.values and "to" not in form.values


def test_a_period_and_a_start_both_narrow_the_later_start_wins():
    form = event_service.parse_filter({"period": "7d", "from": (NOW - timedelta(days=3)).date().isoformat()}, now=NOW)
    assert form.criteria.since == datetime.combine(date(2026, 11, 1), datetime.min.time(), UTC)
    form = event_service.parse_filter({"period": "24h", "from": "2026-10-01"}, now=NOW)
    assert form.criteria.since == NOW - timedelta(hours=24)


def test_a_development_counts_from_the_chosen_start_without_a_period():
    form = event_service.parse_filter({"development": "new", "from": "2026-10-01"}, now=NOW)
    assert "period" not in form.values
    assert form.criteria.since == datetime(2026, 10, 1, tzinfo=UTC)
    # Without a start the shortest period, as before.
    assert event_service.parse_filter({"development": "new"}, now=NOW).values["period"] == "24h"


def test_the_search_is_cut_and_neither_the_start_page_nor_a_rule_keeps_it():
    form = event_service.parse_filter({"q": "x" * 300, "type": "labor", "from": "2026-10-01"}, now=NOW)
    assert len(form.values["q"]) == event_service.SEARCH_MAX_CHARS
    assert form.rule_query == "type=labor"
    assert event_service.parse_filter({"q": "strike", "to": "2026-10-01"}, with_period=False).criteria.text is None
    assert event_service.parse_filter({"q": "strike"}, now=NOW).rule_query == ""
