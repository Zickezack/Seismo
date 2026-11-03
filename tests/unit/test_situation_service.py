"""The rules of the Lagen without a database: comparing, grouping, placing, and reading the LLM's answers."""
import json
from types import SimpleNamespace

import pytest

from app.services import situation_service
from app.services.situation_service import Placement, UnusableAnswer


def report(event_id, title, summary=""):
    return SimpleNamespace(event_id=event_id, title=title, summary=summary, place=None, actors=[])


def mention(event_id, entity_id):
    return SimpleNamespace(event_id=event_id, entity_id=entity_id)


BACKGROUND = [report(100 + number, title) for number, title in enumerate((
    "Floods cut rail lines in Valencia", "Dock workers strike in Rotterdam", "Earthquake strikes Xizang",
    "Wildfire spreads north of Athens", "Nvidia reports record quarterly revenue", "Chile elects a new president",
))]


def test_an_event_s_document_has_its_reports_words_and_its_entities_weighted():
    found = situation_service.documents([report(1, "Houthis attack a tanker", "Houthi rebels fired")], [mention(1, 7), mention(1, 7)])

    assert found[1]["#7"] == 2 * situation_service.ENTITY_WEIGHT
    assert found[1]["houthis"] == 1 and found[1]["tanker"] == 1


def test_a_shared_rare_entity_makes_events_similar_a_common_word_does_not():
    reports = [report(1, "Strike on a port"), report(2, "Strike on a port"), *BACKGROUND]
    without = situation_service.vectors(situation_service.documents(reports, []))
    shared = situation_service.vectors(situation_service.documents(reports, [mention(1, 7), mention(2, 7)]))

    assert situation_service.similarity(shared[1], shared[2]) > situation_service.similarity(without[1], without[2]) - 1e-9
    assert situation_service.similarity(shared[1], shared[100]) < situation_service.similarity(shared[1], shared[2])


def test_a_profile_is_a_unit_vector_closer_to_its_events_than_to_others():
    vectors = situation_service.vectors(situation_service.documents(
        [report(1, "Houthis attack a tanker in the Red Sea"), report(2, "Houthi drone hits a ship in the Red Sea"), *BACKGROUND], [],
    ))
    centre = situation_service.profile([vectors[1], vectors[2]])

    assert sum(value * value for value in centre.values()) == pytest.approx(1)
    assert situation_service.similarity(centre, vectors[1]) > situation_service.similarity(centre, vectors[100])
    assert situation_service.profile([]) == {}


def test_groups_are_led_by_the_event_with_the_most_similar_others_and_need_three():
    unit = situation_service._unit
    vectors = {
        1: unit({"a": 1, "b": 1}), 2: unit({"a": 1, "b": 1, "c": 1}), 3: unit({"a": 1, "c": 1}), 4: unit({"a": 1, "b": 1, "d": 1}),
        5: unit({"x": 1, "y": 1}), 6: unit({"x": 1, "z": 1}), 7: unit({"q": 1}), 8: unit({"r": 1}), 9: unit({"s": 1}),
        10: unit({"t": 1}), 11: unit({"u": 1}), 12: unit({"v": 1}), 13: unit({"w": 1}), 14: unit({"o": 1}), 15: unit({"p": 1}),
        16: unit({"k": 1}),
    }

    [group] = situation_service.groups(vectors, link=0.4)

    # 1 and 2 each have three similar others, the lower id leads; 5 and 6 are alike, but two are no group.
    assert group[0] == 1 and set(group) == {1, 2, 3, 4}


def test_a_term_most_events_share_links_nothing():
    unit = situation_service._unit
    vectors = {number: unit({"common": 1, f"own{number}": 0.1}) for number in range(10)}

    assert situation_service.groups(vectors, link=0.2) == []


def test_scores_join_from_join_from_and_ask_in_between_the_best_few():
    join, ask = situation_service.JOIN_FROM, situation_service.ASK_FROM
    scores = {1: join, 2: ask, 3: ask + 0.05, 4: ask + 0.01, 5: ask + 0.02, 6: ask - 0.01}

    assert situation_service.place(scores) == Placement([(1, join)], [(3, ask + 0.05), (5, ask + 0.02), (4, ask + 0.01)])
    assert situation_service.place({}) == Placement([], [])


def test_the_lagen_chosen_are_read_each_once():
    assert situation_service.chosen_numbers(json.dumps({"reason": "", "belongs_to": [2, 1, 2]}), 3) == [2, 1]
    assert situation_service.chosen_numbers(json.dumps({"reason": "", "belongs_to": []}), 3) == []
    for content in ("", "{}", json.dumps({"belongs_to": [4]}), json.dumps({"belongs_to": ["1"]}), json.dumps({"belongs_to": 1})):
        with pytest.raises(UnusableAnswer):
            situation_service.chosen_numbers(content, 3)


def answer(**fields):
    values = {"reason": "", "known": None, "situation": True, "name": "  Red  Sea  attacks ", "description": "Houthis.", "events": [1, 3]}
    return json.dumps(values | fields)


def test_a_proposal_is_read_with_its_name_tidied():
    proposal = situation_service.proposal(answer(), events=3, situations=0)

    assert (proposal.situation, proposal.known, proposal.name, proposal.events) == (True, None, "Red Sea attacks", [1, 3])
    assert situation_service.proposal(answer(known=2), events=3, situations=2).known == 2


@pytest.mark.parametrize("content", [
    answer(known=1), answer(events=[4]), answer(situation="yes"), answer(name=None), "not json", json.dumps({"known": None}),
])
def test_an_unusable_proposal_is_retried(content):
    with pytest.raises(UnusableAnswer):
        situation_service.proposal(content, events=3, situations=0)


def test_the_questions_name_the_events_and_the_known_lagen_with_some_of_their_events():
    from datetime import UTC, datetime

    moment = datetime(2026, 11, 1, tzinfo=UTC)
    event = SimpleNamespace(id=5, title="Houthis hit a tanker", first_reported_at=moment, last_reported_at=moment, latest_summary="A tanker burned.")
    lage = SimpleNamespace(id=9, name="Houthi attacks", description="Attacks on ships.")

    [system, user] = situation_service.assign_messages(event, [lage], {9: ["Drone hits a ship", "Missile at a tanker"]})

    assert system["content"] == situation_service.ASSIGN_PROMPT
    assert user["content"] == (
        "Event:\nHouthis hit a tanker (reported 2026-11-01 to 2026-11-01)\n   A tanker burned.\n\n"
        "Known situations:\n1. Houthi attacks: Attacks on ships.\n   Some of its events: Drone hits a ship; Missile at a tanker"
    )
    schema = situation_service.propose_schema(events=3, situations=0)
    assert schema["properties"]["known"] == {"enum": [None]} and schema["properties"]["events"]["items"] == {"enum": [1, 2, 3]}
