"""The extraction's rules without a database or a model: the text the model sees, the quotes that back a statement,
what is kept of an answer, the instructions."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db.models import EventType, ValidationResult
from app.llm import event_schema
from app.services import extraction_service
from app.llm import ollama
from app.services.extraction_service import RESERVED_TOKENS, backed_candidates, find_quote, fit_text, user_message

TEXT = (
    "Ein Erdbeben der Stärke 5,5 hat am Donnerstag die Küste Siziliens erschüttert.\n\n"
    "Der Zivilschutz meldet „keine Verletzten“ – Schäden gebe es an zwei Kirchen in Messina."
)


def test_a_text_within_the_context_is_kept_whole():
    assert fit_text(TEXT, 8192) == (TEXT, False)


def test_a_long_text_is_cut_at_a_word_s_end_to_the_context_left():
    words = "Wort " * 6000  # 30000 ASCII characters, about 10000 tokens
    text, cut = fit_text(words, 8192)

    assert cut and text.endswith("Wort") and len(text) / 3 <= 8192 - RESERVED_TOKENS


def test_a_character_outside_ascii_counts_as_a_token_of_its_own():
    chinese = "台风过境造成停电" * 1000

    text, cut = fit_text(chinese, 8192)

    assert cut and len(text) == 8192 - RESERVED_TOKENS


def test_a_quote_is_found_as_it_stands_despite_spaces_and_typographic_marks():
    # The model writes plain quotes, a plain dash and one line instead of the paragraph break.
    assert find_quote('Der Zivilschutz meldet "keine Verletzten" - Schäden', TEXT) == "Der Zivilschutz meldet „keine Verletzten“ – Schäden"
    assert find_quote("Siziliens erschüttert.  Der Zivilschutz", TEXT) == "Siziliens erschüttert.\n\nDer Zivilschutz"


def test_a_quote_is_found_without_its_marks_and_punctuation_at_the_ends_and_regardless_of_case():
    # The model quotes one sentence of a passage the article quotes as a whole, and starts it with a capital.
    text = "Sie sagten: „Da tempo denunciamo il degrado. quello di ieri è l’ennesimo intervento.“ Dann gingen sie."

    assert find_quote('"Da tempo denunciamo il degrado."', text) == "Da tempo denunciamo il degrado."
    # Punctuation the passage lacks there: found without it.
    assert find_quote('"Sie sagten."', text) == "Sie sagten"
    assert find_quote("\"Quello di ieri è l'ennesimo intervento\".", text) == "quello di ieri è l’ennesimo intervento"


def test_in_a_script_without_spaces_a_few_characters_are_quote_enough():
    text = "美國和以色列醞釀在美國期中選舉過後重啟對伊朗攻擊。"

    assert find_quote("美國期中選舉過後", text) == "美國期中選舉過後"
    assert find_quote("美國", text) is None


def test_a_quote_not_in_the_text_or_too_short_backs_nothing():
    assert find_quote("Ein Erdbeben der Stärke 6,1", TEXT) is None
    assert find_quote("An earthquake of magnitude 5.5", TEXT) is None  # translated
    assert find_quote("Messina", TEXT) is None


def answer(*events):
    return event_schema.parse(json.dumps({"events": list(events)}))


def event(*quotes, title="Earthquake off Sicily", confidence="high", **fields):
    return {
        "event_type": "natural_disaster", "status": "start", "confidence": "medium", "title": title,
        "summary": "A quake hit Sicily.",
        "location": {"country": "IT", "place": "Sicily", "quote": "die Küste Siziliens erschüttert"},
        "time_note": "on Thursday", "time_quote": "am Donnerstag die Küste",
        "actors": [{"name": "Civil protection", "role": "reports damage", "quote": "Der Zivilschutz meldet"}],
        "impacts": [{"target": "Messina", "target_kind": "other", "direction": "negative", "horizon": "days",
                     "description": "Two churches damaged.", "quote": "Schäden gebe es an zwei Kirchen in Messina"}],
        "statements": [{"kind": "fact", "text": f"Statement {number}.", "quote": quote, "confidence": confidence}
                       for number, quote in enumerate(quotes)],
    } | fields


def test_statements_without_a_found_quote_are_dropped_and_events_left_without_one():
    candidates, dropped, details_dropped = backed_candidates(
        answer(event("Ein Erdbeben der Stärke 5,5", "gibt es nicht im Text"), event("auch das nicht", title="Other")), TEXT,
        quality="high",
    )

    assert (dropped, details_dropped) == (2, 0)
    [candidate] = candidates
    assert candidate["statements"] == [{"kind": "fact", "text": "Statement 0.", "quote": "Ein Erdbeben der Stärke 5,5",
                                        "confidence": "high", "source_quality": "high"}]
    assert (candidate["country"], candidate["place"], candidate["location_quote"]) == ("IT", "Sicily", "die Küste Siziliens erschüttert")
    assert (candidate["time_note"], candidate["time_quote"], candidate["inference_confidence"]) == ("on Thursday", "am Donnerstag die Küste", "medium")
    assert candidate["actors"] == [{"name": "Civil protection", "role": "reports damage", "quote": "Der Zivilschutz meldet"}]
    assert candidate["impacts"][0]["quote"] == "Schäden gebe es an zwei Kirchen in Messina"


def test_a_detail_without_a_found_quote_is_unknown():
    unbacked = event(
        "Ein Erdbeben der Stärke 5,5",
        location={"country": "IT", "place": "Palermo", "quote": "in Palermo bebte die Erde"},
        time_quote=None,
        actors=[{"name": "Mayor", "role": "speaks", "quote": "Der Bürgermeister sagte"}],
        impacts=[{"target": "Tourism", "target_kind": "sector", "direction": "negative", "horizon": "weeks",
                  "description": "", "quote": "Der Tourismus leidet"}],
    )

    [candidate], dropped, details_dropped = backed_candidates(answer(unbacked), TEXT)

    assert (candidate["country"], candidate["place"], candidate["location_quote"]) == (None, None, None)
    assert (candidate["time_note"], candidate["time_quote"]) == (None, None)
    assert (candidate["actors"], candidate["impacts"], dropped, details_dropped) == ([], [], 0, 4)


def test_a_cut_text_or_a_repaired_answer_lowers_every_extraction_level_by_one():
    levels = [event(quote, confidence=level) for quote, level in (
        ("Ein Erdbeben der Stärke 5,5", "high"), ("Der Zivilschutz meldet", "medium"), ("zwei Kirchen in Messina", "low")
    )]

    candidates, _dropped, _details = backed_candidates(answer(*levels), TEXT, lowered=True)

    assert [candidate["statements"][0]["confidence"] for candidate in candidates] == ["medium", "low", "low"]
    # The inference level is the model's own: a cut text says nothing about how sure a type follows.
    assert {candidate["inference_confidence"] for candidate in candidates} == {"medium"}


@pytest.mark.parametrize(
    ("url", "source_level", "expected"),
    [
        ("https://www.ecb.europa.eu/press/pr.html", None, "high"),  # under a listed domain
        ("https://news.europa.eu/a", "low", "medium"),  # the most specific domain, before the source's own
        ("https://blog.example.org/a", "low", "low"),  # no domain listed: the source's
        ("https://blog.example.org/a", None, None),  # neither: unknown
    ],
)
def test_source_quality_by_domain_else_the_source_s_else_unknown(url, source_level, expected):
    levels = {"ecb.europa.eu": "high", "europa.eu": "low", "news.europa.eu": "medium"}

    assert extraction_service.source_quality(url, source_level, levels) == expected


def test_asked_again_the_model_learns_what_was_wrong_but_not_its_answer():
    article = SimpleNamespace(source_name="ANSA", published_at=None, title="Terremoto")
    invalid = extraction_service.Failure(ValidationResult.INVALID, "events.0.event_type: Input should be 'armed_conflict', ...")
    cut = extraction_service.Failure(ValidationResult.INCOMPLETE, "cut off after 3000 tokens")

    message = extraction_service.messages(article, TEXT, invalid)[1]["content"]

    assert message.endswith("A previous answer was not usable: it did not match the schema:\n"
                            "events.0.event_type: Input should be 'armed_conflict', ...\nAnswer again: the complete JSON in the schema.")
    assert "answer shorter, with fewer events and statements" in extraction_service.messages(article, TEXT, cut)[1]["content"]


def test_the_errors_of_an_answer_name_where_and_what_never_the_value():
    too_long = event("Ein Erdbeben der Stärke 5,5") | {"title": "Geheimer Artikeltext " * 20}
    with pytest.raises(ValidationError) as error:
        event_schema.parse(json.dumps({"events": [too_long, {"event_type": "flood"}]}))

    summary = event_schema.error_summary(error.value, limit=3)

    assert summary.splitlines()[0] == "events.0.title: String should have at most 200 characters"
    assert "Geheimer" not in summary and summary.endswith("more")


def test_the_instructions_name_every_event_type_and_the_header_the_article():
    assert all(event_type in extraction_service.SYSTEM_PROMPT for event_type in EventType)
    article = SimpleNamespace(source_name="ANSA", published_at=datetime(2026, 10, 23, 9, tzinfo=UTC), title="Terremoto")

    assert user_message(article, TEXT).startswith("Source: ANSA\nPublished: 2026-10-23\nTitle: Terremoto\n\nArticle text:\nEin Erdbeben")


def test_the_schema_has_no_references_and_refuses_what_it_does_not_know():
    schema = json.dumps(event_schema.json_schema())

    assert "$ref" not in schema and "$defs" not in schema
    unknown = event("Ein Erdbeben der Stärke 5,5", severity="high")
    for invalid in ({"events": [unknown]}, {"events": [event("Ein Erdbeben der Stärke 5,5") | {"event_type": "flood"}]}):
        try:
            event_schema.parse(json.dumps(invalid))
        except ValueError:
            continue
        raise AssertionError(f"accepted {invalid}")


def test_the_answer_may_take_more_than_the_room_a_text_leaves_it():
    # A short text leaves the answer the rest of the context, up to the cap; a long one at least the room reserved.
    assert ollama.MAX_ANSWER_TOKENS > ollama.MIN_ANSWER_ROOM_TOKENS
    assert RESERVED_TOKENS == 1000 + ollama.MIN_ANSWER_ROOM_TOKENS
