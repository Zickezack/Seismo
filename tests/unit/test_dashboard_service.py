"""The start page's rules without a database: the period, and which versions are changes (dashboard_service)."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import dashboard_service

SINCE = datetime(2026, 10, 30, 12, tzinfo=UTC)


def version(event_id, status, hours_after_since):
    return SimpleNamespace(event_id=event_id, status=status, valid_from=SINCE + timedelta(hours=hours_after_since))


def test_an_unknown_period_is_the_default():
    assert [dashboard_service.parse_period(raw) for raw in ("3d", "7d", "", None, "1y")] == ["3d", "7d", "24h", "24h", "24h"]


def test_a_change_is_a_version_within_the_period_with_another_status_than_the_one_before():
    versions = [
        # Founded before, escalated within: a change.
        version(1, "start", -10), version(1, "escalation", 2),
        # Founded within: its founding is no change, its closure is.
        version(2, "start", 1), version(2, "closure", 3),
        # An earlier report found later repeats the state: no change.
        version(3, "update", -5), version(3, "update", 4),
        # Changed before the period only.
        version(4, "start", -9), version(4, "closure", -1),
    ]

    changes = dashboard_service.changes_since(versions, SINCE)

    assert {event_id: [change.status_label for change in found] for event_id, found in changes.items()} == {
        1: ["Eskalation"], 2: ["Abschluss"],
    }
