"""The assignment's rules without a database or a model: the words compared, the similarity, what the scores decide
alone, the question to the LLM and its answer."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.db.models import AssignmentMethod
from app.services import event_service
from app.services.event_service import ASSIGN_FROM, NEW_BELOW, Decision, Match, UnusableChoice, chosen_number, matches, settled


def report(title, summary, event_id=None, article_id=None, place=None, actors=(), **fields):
    return SimpleNamespace(title=title, summary=summary, place=place, actors=[{"name": name} for name in actors],
                           event_id=event_id, article_id=article_id, **fields)


QUAKE = report("Earthquake of magnitude 6.8 strikes Xizang", "A strong earthquake hit Dingri county in Xizang, killing 126.",
               place="Dingri", actors=["China Earthquake Networks Center"])
AFTERSHOCK = report("Aftershocks hamper rescue in Xizang quake zone", "Rescuers in Dingri county search for survivors of the "
                    "Xizang earthquake as aftershocks continue.", event_id=1, article_id=10, place="Dingri")
RATES = report("ECB keeps rates unchanged", "The European Central Bank left its key interest rates unchanged.", event_id=2,
               article_id=20, actors=["ECB"])
BANK = report("Bank reports quarterly profit", "The bank reported a rise in quarterly net profit.", event_id=3, article_id=30)


def test_the_words_of_a_report_leave_out_stop_words_short_ones_and_punctuation():
    words = event_service.words(report("Strike at the port of Rotterdam", "Dockers said: the strike ends on Monday.",
                                       place="Rotterdam", actors=["FNV Havens"]))

    assert words == {"strike": 2, "port": 1, "rotterdam": 2, "dockers": 1, "ends": 1, "monday": 1, "fnv": 1, "havens": 1}


def test_the_most_similar_event_comes_first_and_an_unrelated_one_scores_nothing_shared():
    found = matches(QUAKE, [AFTERSHOCK, RATES, BANK])

    assert [match.event_id for match in found] == [1, 2, 3]
    # A continuation in words of its own: in the grey zone, where the LLM decides.
    assert NEW_BELOW <= found[0].score < ASSIGN_FROM and found[1].score == found[2].score == 0


def test_an_event_scores_what_its_most_similar_report_does():
    other_report_of_the_quake = report("Death toll rises", "Officials count more victims.", event_id=1, article_id=11)

    alone, together = matches(QUAKE, [AFTERSHOCK]), matches(QUAKE, [AFTERSHOCK, other_report_of_the_quake])

    assert together[0].event_id == 1 and together[0].score >= alone[0].score - 0.05


def test_an_event_with_a_report_of_the_same_article_is_left_out():
    same_article = SimpleNamespace(**vars(QUAKE) | {"article_id": 10})

    assert [match.event_id for match in matches(same_article, [AFTERSHOCK, RATES])] == [2]


def test_with_few_reports_a_word_they_all_share_still_counts():
    # One report to compare with: a word both have is in every document, its weight still above zero.
    only = report("Earthquake strikes Xizang", "An earthquake hit Xizang.", event_id=1, article_id=10)

    [found] = matches(report("Earthquake in Xizang", "Xizang earthquake."), [only])

    assert found.score > ASSIGN_FROM


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        ([Match(4, ASSIGN_FROM), Match(5, 0.3)], Decision(AssignmentMethod.SCORE, 4, ASSIGN_FROM)),
        ([Match(4, NEW_BELOW - 0.01)], Decision(AssignmentMethod.NEW)),
        ([], Decision(AssignmentMethod.NEW)),
        ([Match(4, ASSIGN_FROM - 0.01)], None),
        ([Match(4, NEW_BELOW)], None),
    ],
    ids=["similar enough", "nothing alike", "no event near", "just below assigning", "just at the grey zone"],
)
def test_the_scores_decide_alone_except_in_between(found, expected):
    assert settled(found) == expected


def test_in_between_the_llm_chooses_among_the_three_best_from_the_grey_zone_on():
    found = [Match(1, 0.45), Match(2, 0.3), Match(3, 0.25), Match(4, 0.22), Match(5, 0.1)]

    assert [match.event_id for match in event_service.choices(found)] == [1, 2, 3]
    assert [match.event_id for match in event_service.choices([Match(1, 0.3), Match(2, 0.1)])] == [1]


def test_the_question_shows_the_report_and_every_known_event_with_its_time_and_latest_report():
    candidate = SimpleNamespace(title="Aftershocks in Xizang", summary="Rescue goes on.", place="Dingri", country="CN",
                                time_note=None, moment=datetime(2026, 1, 8, 9, tzinfo=UTC))
    events = [SimpleNamespace(title="Earthquake strikes Xizang", first_reported_at=datetime(2026, 1, 7, tzinfo=UTC),
                              last_reported_at=datetime(2026, 1, 7, 18, tzinfo=UTC), latest_summary="126 dead.")]

    system, user = event_service.choice_messages(candidate, events)

    assert "not the same event" in system["content"].lower()
    assert user["content"] == (
        "Report, published 2026-01-08:\nTitle: Aftershocks in Xizang\nSummary: Rescue goes on.\nPlace: Dingri, CN\n"
        "Time: unknown\n\nKnown events:\n1. Earthquake strikes Xizang (reported 2026-01-07 to 2026-01-07)\n"
        "   Latest report: 126 dead."
    )


def test_the_answer_may_only_name_a_known_event_or_none():
    schema = event_service.choice_schema(2)

    assert schema["properties"]["same_as"] == {"enum": [1, 2, None]}
    assert chosen_number(json.dumps({"reason": "Same quake.", "same_as": 2}), 2) == 2
    assert chosen_number(json.dumps({"reason": "Another one.", "same_as": None}), 2) is None


@pytest.mark.parametrize("content", ['{"same_as": 3}', '{"same_as": 0}', '{"same_as": true}', '{"same_as": "1"}', "{}", '{"same_as": ', ""])
def test_an_answer_naming_no_known_event_is_unusable(content):
    with pytest.raises(UnusableChoice):
        chosen_number(content, 2)


@pytest.mark.parametrize(
    ("method", "score", "label"),
    [(AssignmentMethod.NEW, None, "Neues Ereignis"), (AssignmentMethod.SCORE, 0.618, "Ähnlichkeit 0,62"),
     (AssignmentMethod.LLM, 0.31, "LLM (Ähnlichkeit 0,31)")],
)
def test_a_report_shows_how_it_came_to_its_event(method, score, label):
    view = event_service.ReportView(
        id=1, title="t", status="update", assignment_method=method, assignment_score=score, article_id=1,
        article_title=None, article_url="https://a.example/1", moment=datetime(2026, 1, 1, tzinfo=UTC), source_display_name="Q",
    )

    assert (view.method_label, view.status_label) == (label, "Fortsetzung")
