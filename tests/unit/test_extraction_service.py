"""The extraction's rules without a database or a model: the text the model sees, the quotes that back a statement,
what is kept of an answer, the instructions."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from app.db.models import EventType
from app.llm import event_schema
from app.services import extraction_service
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


def test_a_quote_not_in_the_text_or_too_short_backs_nothing():
    assert find_quote("Ein Erdbeben der Stärke 6,1", TEXT) is None
    assert find_quote("An earthquake of magnitude 5.5", TEXT) is None  # translated
    assert find_quote("Messina", TEXT) is None


def answer(*events):
    return event_schema.parse(json.dumps({"events": list(events)}))


def event(*quotes, title="Earthquake off Sicily"):
    return {
        "event_type": "natural_disaster", "status": "start", "title": title, "summary": "A quake hit Sicily.",
        "location": {"country": "IT", "place": "Sicily"}, "time_note": "on Thursday",
        "actors": [{"name": "Zivilschutz", "role": "civil protection"}],
        "impacts": [{"target": "Messina", "target_kind": "other", "direction": "negative", "horizon": "days", "description": ""}],
        "statements": [{"kind": "fact", "text": f"Statement {number}.", "quote": quote} for number, quote in enumerate(quotes)],
    }


def test_statements_without_a_found_quote_are_dropped_and_events_left_without_one():
    candidates, dropped = backed_candidates(
        answer(event("Ein Erdbeben der Stärke 5,5", "gibt es nicht im Text"), event("auch das nicht", title="Other")), TEXT
    )

    assert dropped == 2
    [candidate] = candidates
    assert candidate["statements"] == [{"kind": "fact", "text": "Statement 0.", "quote": "Ein Erdbeben der Stärke 5,5"}]
    assert (candidate["country"], candidate["place"], candidate["time_note"]) == ("IT", "Sicily", "on Thursday")
    assert candidate["impacts"][0]["direction"] == "negative" and candidate["actors"] == [{"name": "Zivilschutz", "role": "civil protection"}]


def test_the_instructions_name_every_event_type_and_the_header_the_article():
    assert all(event_type in extraction_service.SYSTEM_PROMPT for event_type in EventType)
    article = SimpleNamespace(source_name="ANSA", published_at=datetime(2026, 10, 23, 9, tzinfo=UTC), title="Terremoto")

    assert user_message(article, TEXT).startswith("Source: ANSA\nPublished: 2026-10-23\nTitle: Terremoto\n\nArticle text:\nEin Erdbeben")


def test_the_schema_has_no_references_and_refuses_what_it_does_not_know():
    schema = json.dumps(event_schema.json_schema())

    assert "$ref" not in schema and "$defs" not in schema
    unknown = event("Ein Erdbeben der Stärke 5,5") | {"severity": "high"}
    for invalid in ({"events": [unknown]}, {"events": [event("Ein Erdbeben der Stärke 5,5") | {"event_type": "flood"}]}):
        try:
            event_schema.parse(json.dumps(invalid))
        except ValueError:
            continue
        raise AssertionError(f"accepted {invalid}")
