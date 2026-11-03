"""The rules of the alerts without a database: the throttle, and the rule form."""
from datetime import UTC, datetime, timedelta

import pytest

from app.services import alert_service, event_service
from app.services.alert_service import Delivery, InvalidRule, Waiting

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
QUIET = timedelta(hours=6)


def hit(hit_id, event_id, trigger="new", ago=MINUTE):
    return Waiting(hit_id, event_id, trigger, NOW - ago)


def throttle(waiting, last_alerted=None, sent_last_hour=0, max_per_hour=5, cooldown=QUIET):
    return alert_service.throttle(waiting, last_alerted or {}, sent_last_hour, max_per_hour, cooldown, NOW)


def test_an_event_s_hits_go_out_together_one_alert_per_event():
    waiting = [hit(1, 10), hit(2, 20), hit(3, 10, "report")]

    assert throttle(waiting) == [Delivery([1, 3]), Delivery([2])]


def test_an_event_alerted_within_the_quiet_period_waits_unless_it_changed_its_state():
    waiting = [hit(1, 10, "report"), hit(2, 20, "report"), hit(3, 20, "change")]

    assert throttle(waiting, last_alerted={10: NOW - HOUR, 20: NOW - HOUR}) == [Delivery([2, 3])]
    # Its end lets it go.
    assert throttle(waiting[:1], last_alerted={10: NOW - QUIET}) == [Delivery([1])]
    # Without a quiet period, nothing waits for one.
    assert throttle(waiting[:1], last_alerted={10: NOW - MINUTE}, cooldown=timedelta(0)) == [Delivery([1])]


def test_changes_of_state_go_first_then_the_earliest_found():
    waiting = [hit(1, 10, ago=3 * MINUTE), hit(2, 20, ago=2 * MINUTE), hit(3, 30, "change", ago=MINUTE)]

    assert throttle(waiting) == [Delivery([3]), Delivery([1]), Delivery([2])]


def test_beyond_the_hourly_limit_the_last_alert_allowed_takes_the_rest_as_a_digest():
    waiting = [hit(each, each * 10, ago=(10 - each) * MINUTE) for each in range(1, 6)]

    assert throttle(waiting, max_per_hour=3) == [Delivery([1]), Delivery([2]), Delivery([3, 4, 5], digest=True)]
    assert throttle(waiting, max_per_hour=3, sent_last_hour=2) == [Delivery([1, 2, 3, 4, 5], digest=True)]
    # At the limit everything waits for the hour to allow more.
    assert throttle(waiting, max_per_hour=3, sent_last_hour=3) == []


def test_nothing_waiting_sends_nothing():
    assert throttle([]) == []


FORM = {"name": "  Häfen  in Europa ", "is_active": "1", "on_new": "1", "max_per_hour": "5", "cooldown_hours": "6"}


def test_a_rule_keeps_the_filter_of_the_event_list_without_period():
    values = alert_service.parse(FORM | {"watchlist": "3", "country": "nl", "type": "labor", "sources": "3", "impact": "1",
                                         "period": "7d", "development": "new", "sector": "x"})

    assert values.name == "Häfen in Europa"
    assert values.watchlist_id == 3
    # Only known values: an unknown sector narrows nothing, period and development belong to the list.
    assert values.criteria == {"country": "NL", "type": "labor", "sources": "3", "impact": "1"}
    assert (values.on_new, values.on_change, values.on_report, values.is_active) == (True, False, False, True)


@pytest.mark.parametrize(("change", "message"), [
    ({"name": " "}, "Name: bitte angeben."),
    ({"name": "x" * 201}, "Name: höchstens 200 Zeichen."),
    ({"on_new": ""}, "Melden bei: bitte mindestens eines wählen."),
    ({"max_per_hour": "0"}, "Höchstens pro Stunde: eine ganze Zahl von 1 bis 100."),
    ({"max_per_hour": "101"}, "Höchstens pro Stunde: eine ganze Zahl von 1 bis 100."),
    ({"cooldown_hours": "-1"}, "Ruhezeit pro Ereignis (Stunden): eine ganze Zahl von 0 bis 168."),
    ({"cooldown_hours": "169"}, "Ruhezeit pro Ereignis (Stunden): eine ganze Zahl von 0 bis 168."),
    ({"watchlist": "a"}, "Watchlist: bitte eine aus der Liste wählen."),
])
def test_an_invalid_entry_names_its_field(change, message):
    with pytest.raises(InvalidRule) as raised:
        alert_service.parse(FORM | change)
    assert str(raised.value) == message


def test_a_new_rule_starts_with_the_filter_it_was_asked_from():
    values = alert_service.new_rule({"type": "labor", "watchlist": "4", "period": "3d"})

    assert values["criteria"] == {"type": "labor"} and values["watchlist_id"] == 4
    assert (values["max_per_hour"], values["cooldown_hours"], values["on_new"], values["on_report"]) == (5, 6, True, False)


def test_the_filter_reads_a_watchlist_from_the_url():
    assert event_service.parse_filter({"watchlist": "12"}).criteria.watchlist_id == 12
    assert event_service.parse_filter({"watchlist": "zwölf"}).criteria.watchlist_id is None
