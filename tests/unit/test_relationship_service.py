"""The rules of relationships without a database: which entities can stand in them, how they are stored, and how the
Angaben make their state (app/services/relationship_service.py)."""
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import relationship_service
from app.services.relationship_service import State


def report(day, ended=False):
    return SimpleNamespace(origin="report", reported_on=day, ended=ended)


def analyst(valid_from=None, valid_to=None, review="confirmed"):
    return SimpleNamespace(origin="analyst", valid_from=valid_from, valid_to=valid_to, review=review)


def states(*records):
    return [(state.valid_from, state.valid_to, state.review, changed) for _, state, changed in relationship_service.replay(records)]


OCT_1, OCT_10, OCT_20, OCT_30 = date(2026, 10, 1), date(2026, 10, 10), date(2026, 10, 20), date(2026, 10, 30)


@pytest.mark.parametrize(("kind", "subject", "object_", "allowed"), [
    ("supplies", "company", "company", True),
    ("supplies", "country", "country", True),  # Russia supplies gas to Germany
    ("supplies", "company", "authority", True),  # a supplier of government agencies
    ("supplies", "person", "company", False),
    ("competes", "subsidiary", "company", True),
    ("competes", "country", "country", False),
    ("regulates", "authority", "company", True),
    ("regulates", "organization", "sector", True),
    ("regulates", "company", "company", False),
    ("depends_on", "sector", "commodity", True),
    ("depends_on", "company", "company", False),
])
def test_which_entities_can_stand_in_a_relationship(kind, subject, object_, allowed):
    assert relationship_service.allowed(kind, subject, object_) is allowed


def test_a_competition_is_stored_with_the_lower_id_first_every_other_as_given():
    assert relationship_service.normalized("competes", 9, 4) == (4, 9, True)
    assert relationship_service.normalized("competes", 4, 9) == (4, 9, False)
    assert relationship_service.normalized("supplies", 9, 4) == (9, 4, False)


def test_a_report_that_it_holds_opens_it_and_later_ones_confirm():
    assert states(report(OCT_10), report(OCT_20)) == [(OCT_10, None, "unreviewed", True), (OCT_10, None, "unreviewed", False)]


def test_an_earlier_report_found_later_moves_the_start_and_an_end_closes_it():
    assert states(report(OCT_20), report(OCT_10), report(OCT_30, ended=True)) == [
        (OCT_20, None, "unreviewed", True), (OCT_10, None, "unreviewed", True), (OCT_10, OCT_30, "unreviewed", True),
    ]


def test_an_end_before_the_start_or_twice_changes_nothing():
    assert states(report(OCT_20), report(OCT_10, ended=True))[-1] == (OCT_20, None, "unreviewed", False)
    assert states(report(OCT_10), report(OCT_20, ended=True), report(OCT_30, ended=True))[-1] == (OCT_10, OCT_20, "unreviewed", False)


def test_a_report_that_it_holds_after_its_end_opens_it_again():
    assert states(report(OCT_1), report(OCT_10, ended=True), report(OCT_20))[-1] == (OCT_20, None, "unreviewed", True)


def test_a_relationship_known_first_by_its_end_has_an_unknown_start():
    assert states(report(OCT_10, ended=True)) == [(None, OCT_10, "unreviewed", True)]


def test_the_analyst_sets_dates_and_review_and_reports_never_change_the_review():
    assert states(report(OCT_10), analyst(OCT_1, None, "disputed"), report(OCT_20)) == [
        (OCT_10, None, "unreviewed", True), (OCT_1, None, "disputed", True), (OCT_1, None, "disputed", False),
    ]


def test_a_disputed_or_ended_relationship_is_not_in_effect_and_says_its_validity():
    assert State(OCT_1).in_effect and State(OCT_1).validity_label == "seit 01.10.2026"
    assert not State(OCT_1, review="disputed").in_effect
    assert not State(OCT_1, OCT_10).in_effect and State(OCT_1, OCT_10).validity_label == "01.10.2026 bis 10.10.2026"
    assert State(valid_to=OCT_10).validity_label == "unbekannt bis 10.10.2026"
    assert State().validity_label == "besteht, Beginn unbekannt"


def test_each_kind_has_a_role_from_either_side():
    assert {(kind, side) for kind, side in relationship_service.ROLE_LABELS} == {
        (kind, side) for kind in relationship_service.SIDES for side in (True, False)
    }
    assert relationship_service.ROLE_LABELS[("supplies", True)] == "Kunde"
    assert relationship_service.ROLE_LABELS[("supplies", False)] == "Lieferant"
