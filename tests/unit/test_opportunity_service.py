"""The Chancen's rules without a database (opportunity_service): what a move since the Ereignistag makes of a
Wirkungsannahme, the criteria from the URL, and the order."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.event_study_service import Move, Status
from app.services.opportunity_service import Criteria, Kind, Opportunity, kind_of, order, parse


def move(t, status=Status.DONE):
    return Move(1, "SHEL", "XLE", status, None if t is None else t * 0.01, t, 5, True)


def test_a_move_within_its_noise_or_against_the_direction_is_a_chance_one_with_it_is_not():
    assert kind_of("negative", False, move(-0.6)) == Kind.QUIET
    assert kind_of("negative", False, move(0.99)) == Kind.QUIET
    assert kind_of("negative", False, move(1.4)) == Kind.AGAINST
    assert kind_of("negative", False, move(-1.0)) == Kind.MOVED
    assert kind_of("positive", None, move(2.5)) == Kind.MOVED
    assert kind_of("positive", None, move(-2.5)) == Kind.AGAINST


def test_no_close_yet_is_a_chance_priced_in_unmeasured_or_no_price_is_none():
    assert kind_of("positive", False, move(None, status=None)) == Kind.NO_CLOSE
    assert kind_of("positive", True, move(0.1)) == Kind.PRICED_IN
    assert kind_of("positive", False, move(None, status=Status.GAP)) == Kind.UNMEASURED
    assert kind_of("positive", False, move(None)) == Kind.UNMEASURED  # too few days for the market model
    assert kind_of("positive", False, None) == Kind.NO_PRICE


def test_the_criteria_come_from_the_url_anything_unknown_is_none():
    criteria = parse({"direction": "negative", "probability": "high", "classification": "x", "horizon": "weeks", "sort": "move"})
    assert criteria == Criteria("negative", "high", "", "weeks", "move")
    assert criteria.query() == "direction=negative&probability=high&horizon=weeks&sort=move"
    assert parse({"sort": "best"}).sort == "recent" and parse({}).query() == ""


def hypothesis(id, probability="high", classification="plausible", horizon="months", direction="negative"):
    return SimpleNamespace(id=id, probability=probability, classification=classification, horizon=horizon, direction=direction,
                           likelihood=SimpleNamespace(probability=probability))


def test_the_criteria_match_the_hypothesis():
    assert Criteria(direction="negative", probability="high").matches(hypothesis(1))
    assert not Criteria(horizon="days").matches(hypothesis(1))
    assert not Criteria(classification="evidenced").matches(hypothesis(1))


NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def chance(id, hours_ago, t, **values):
    when = NOW - timedelta(hours=hours_ago)
    return Opportunity(id, "Event", "start", when, when, True, hypothesis(id, **values), move(t), Kind.QUIET)


def test_the_order_is_chosen_never_a_score():
    items = [chance(1, 5, 0.9, probability="low", horizon="days"), chance(2, 1, -0.2, classification="speculative"),
             chance(3, 3, 0.5, classification="evidenced", horizon="years")]
    assert [each.event_id for each in order(items, "recent")] == [2, 3, 1]
    assert [each.event_id for each in order(items, "probability")] == [2, 3, 1]
    assert [each.event_id for each in order(items, "classification")] == [3, 1, 2]
    assert [each.event_id for each in order(items, "move")] == [2, 3, 1]
    assert [each.event_id for each in order(items, "horizon")] == [1, 2, 3]
