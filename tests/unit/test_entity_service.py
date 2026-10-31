"""The rules of the entity recognition without a database: which names are asked about, what the answer makes of
them, what the LLM is shown, and how the pages label it (app/services/entity_service.py)."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.llm import entity_schema
from app.services import entity_service
from app.services.entity_service import AliasView, Category, Known, MentionView, Place

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def candidate(candidate_id, actors=(), impacts=()):
    return SimpleNamespace(
        id=candidate_id, title=f"Report {candidate_id}", summary="Summary.",
        actors=[{"name": name, "role": role, "quote": quote} for name, role, quote in actors],
        impacts=[{"target": target, "target_kind": "company", "direction": "negative", "horizon": "days", "description": "",
                  "quote": quote} for target, quote in impacts],
    )


def item(mention, kind, name=None, **fields):
    """An answer's item; a person with an office, as a person needs one."""
    values = {"mention": mention, "public_role": "President" if kind == "person" else None, "kind": kind, "name": name,
              "short_name": None, "same_as": None, "parent": None, "country_code": None, "category": None, "identifiers": []}
    return values | fields


def answer(*items, relationships=()):
    return entity_schema.parse(json.dumps({"entities": list(items), "relationships": list(relationships)}))


def test_a_name_is_asked_about_once_with_every_place_the_reports_give_it():
    names = entity_service.collect_names([
        candidate(1, actors=[("ECB", "central bank", "Die EZB"), ("Residents", "", "residents")], impacts=[("E.C.B.", "rates")]),
        candidate(2, actors=[("ecb", "rate setter", "the ECB said")]),
    ])

    assert [(name.text, name.key, name.context) for name in names] == [
        ("ECB", "ecb", "actor, role: central bank"), ("Residents", "residents", "actor, role: unknown"),
    ]
    assert names[0].places == [Place(1, "actor", 0), Place(1, "impact", 0), Place(2, "actor", 0)]
    assert names[0].quotes == ["Die EZB", "rates", "the ECB said"]


def test_a_name_without_letters_is_none_and_at_most_max_names_are_asked(monkeypatch):
    monkeypatch.setattr(entity_schema, "MAX_NAMES", 2)

    names = entity_service.collect_names([candidate(1, actors=[("–", "", "q"), ("A", "", "q"), ("B", "", "q"), ("C", "", "q"), ("a", "", "q2")])])

    assert [(name.text, len(name.places)) for name in names] == [("A", 2), ("B", 1)]


def names_of(*texts, quote="no identifier here"):
    return entity_service.collect_names([candidate(1, actors=[(text, "", quote) for text in texts])])


def test_none_a_name_answered_twice_and_an_unknown_number_give_no_entity():
    names = names_of("Residents", "Siemens")

    found = entity_service.recognised(answer(item(1, "none"), item(2, "company", "Siemens"), item(2, "person", "Werner")), names, [], [])

    assert [(each.name.text, each.kind, each.english) for each in found] == [("Siemens", "company", "Siemens")]


def test_a_private_individual_or_a_person_without_an_office_gives_no_entity():
    names = names_of("Olha Chverenchuk", "Paolo Brando", "Friedrich Merz")

    found = entity_service.recognised(answer(
        item(1, "private_person", "Olha Chverenchuk"), item(2, "person", "Paolo Brando", public_role=" "),
        item(3, "person", "Friedrich Merz", public_role="German Chancellor"),
    ), names, [], [])

    assert [each.name.text for each in found] == ["Friedrich Merz"]


def test_a_sector_or_commodity_only_by_a_listed_key():
    names = names_of("Banking sector", "Bitcoin", "Crude")

    found = entity_service.recognised(answer(
        item(1, "sector", "Banking", category="banking"), item(2, "commodity", "Bitcoin", category=None),
        item(3, "commodity", "Crude oil", category="crude-oil"),
    ), names, [], ["banking"])

    assert [(each.name.text, each.identifiers, each.forms) for each in found] == [("Banking sector", [("catalogue", "banking")], ["Banking sector"])]


def test_a_name_without_letters_from_the_model_falls_back_to_the_report_s():
    [found] = entity_service.recognised(answer(item(1, "company", " – ")), names_of("Siemens AG"), [], [])

    assert (found.english, found.forms) == ("Siemens AG", ["Siemens AG"])


def test_the_forms_are_the_report_s_the_usual_english_and_the_short_one_each_once():
    [found] = entity_service.recognised(answer(item(1, "authority", "European Central Bank", short_name="EZB")), names_of("Europäische Zentralbank"), [], [])

    assert found.forms == ["Europäische Zentralbank", "European Central Bank", "EZB"]
    [same] = entity_service.recognised(answer(item(1, "company", "Siemens", short_name="SIEMENS")), names_of("Siemens AG"), [], [])
    assert same.forms == ["Siemens AG"] and same.english == "Siemens"


def test_a_country_needs_an_assigned_code_and_an_identifier_its_quote():
    names = names_of("Ukraine", "Boeing", "EU", quote="Boeing (NYSE: BA) and its ISIN US0378331005")
    identifiers = [{"scheme": "ticker", "value": "NYSE:BA"}, {"scheme": "isin", "value": "US0378331005"},
                   {"scheme": "lei", "value": "HWUPKR0MPOU8FGXBT394"}]  # valid, but not in the quote

    found = entity_service.recognised(answer(
        item(1, "country", "Ukraine", country_code="UA"), item(2, "company", "Boeing", identifiers=identifiers),
        item(3, "country", "European Union", country_code="EU"),
    ), names, [], [])

    assert [each.identifiers for each in found] == [[("iso3166", "UA")], [("ticker", "NYSE:BA"), ("isin", "US0378331005")], []]


def test_same_as_names_the_known_entity_and_a_parent_counts_for_a_subsidiary_or_product_only():
    known = [Known(41, "company", "Alphabet", []), Known(42, "person", "Volodymyr Zelenskyy", [])]
    names = names_of("Vladimir Zelensky", "Google", "Waymo", "Gmail")

    found = entity_service.recognised(answer(
        item(1, "person", "Volodymyr Zelenskyy", same_as=2, parent="Alphabet"), item(2, "subsidiary", "Google", parent="Alphabet"),
        item(3, "company", "Waymo", parent="Alphabet"), item(4, "product", "Gmail", parent="gmail"),
    ), names, known, [])

    assert [(each.same_as, each.parent) for each in found] == [(42, None), (None, "Alphabet"), (None, None), (None, None)]


def test_the_schema_offers_only_the_numbers_and_keys_of_the_question():
    schema = entity_schema.json_schema(2, 1, ["banking"])
    properties = schema["properties"]["entities"]["items"]["properties"]

    assert properties["mention"] == {"enum": [1, 2]}
    assert properties["same_as"] == {"enum": [1, None]}
    assert properties["category"] == {"enum": ["banking", None]}
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)


def test_the_question_numbers_the_names_and_known_entities_and_lists_the_categories():
    candidates = [candidate(1, actors=[("Vladimir Zelensky", "president", "said Vladimir Zelensky")])]
    names = entity_service.collect_names(candidates)
    known = [Known(42, "person", "Volodymyr Zelenskyy", ["Zelensky"])]

    [system, user] = entity_service.messages(candidates, names, known, [Category("banking", "sector", "banking")])

    assert system["content"] == entity_service.SYSTEM_PROMPT
    assert '1. "Vladimir Zelensky" - actor, role: president. Quote: "said Vladimir Zelensky"' in user["content"]
    assert "Known entities:\n1. Volodymyr Zelenskyy (person; also: Zelensky)" in user["content"]
    assert "Listed sectors (category key: what it covers):\nbanking: banking" in user["content"]
    assert "Listed commodities (category key: what it covers):\nnone - use none for any" in user["content"]


def test_company_and_subsidiary_are_matched_together_every_other_kind_alone():
    assert entity_service.matched_kinds("subsidiary") == ("company", "subsidiary")
    assert entity_service.matched_kinds("person") == ("person",)


def alias(**fields):
    values = dict(id=1, scheme="name", value="Volodymyr Zelenskyy", match="llm", similarity=0.857, uncertain=True,
                  confirmed_at=None, created_at=NOW, mention_count=2)
    return AliasView(**values | fields)


def test_an_alias_says_how_it_came_with_its_similarity():
    assert alias().match_label == "Ähnlicher Name, vom LLM als gleich beurteilt (Ähnlichkeit 0,86)"
    assert alias(match="identifier", similarity=None, scheme="iso3166").scheme_label == "ISO-Ländercode"


@pytest.mark.parametrize(("source", "detail", "label"), [
    ("actor", {"role": "President of Ukraine", "quote": "q"}, "Akteur: President of Ukraine"),
    ("actor", {"role": "", "quote": "q"}, "Akteur"),
    ("impact", {"direction": "negative", "horizon": "weeks", "description": "Exports fall.", "quote": "q"}, "Betroffen: negativ, Wochen - Exports fall."),
])
def test_a_mention_says_what_its_report_says_of_the_entity(source, detail, label):
    mention = MentionView(1, source, detail, "Ukraine", False, 2, "Report", 3, "Event", 4, NOW, "Ukrinform")

    assert (mention.role_label, mention.quote) == (label, "q")


def statement(statement_id, text="TSMC supplies chips to Apple.", quote="TSMC liefert Chips an Apple"):
    return SimpleNamespace(id=statement_id, candidate_id=1, text=text, quote=quote, moment=NOW)


def relationship(subject, kind, object_, statement_number=1, ended=False):
    return {"subject": subject, "kind": kind, "object": object_, "ended": ended, "statement": statement_number}


def test_a_stated_relationship_needs_two_different_names_and_a_statement_of_the_question_each_once():
    names = names_of("TSMC", "Apple")
    statements = [statement(7)]
    parsed = answer(item(1, "company", "TSMC"), item(2, "company", "Apple"), relationships=[
        relationship(1, "supplies", 2), relationship(1, "supplies", 2), relationship(1, "competes", 1),
        relationship(2, "competes", 1, ended=True),
    ])

    found = entity_service.stated_relationships(parsed, names, statements)

    assert [(each.subject.text, each.kind, each.object.text, each.ended, each.statement.id) for each in found] == [
        ("TSMC", "supplies", "Apple", False, 7), ("Apple", "competes", "TSMC", True, 7),
    ]


def test_the_question_numbers_the_statements_with_their_quotes():
    candidates = [candidate(1, actors=[("TSMC", "supplier", "TSMC")])]

    [_, user] = entity_service.messages(candidates, entity_service.collect_names(candidates), [], [], [statement(7)])

    assert 'Statements:\n1. TSMC supplies chips to Apple. Quote: "TSMC liefert Chips an Apple"' in user["content"]


def test_without_a_statement_or_a_second_name_the_schema_allows_no_relationship():
    def relationships(names, statements):
        return entity_schema.json_schema(names, 0, [], statements)["properties"]["relationships"]

    assert relationships(2, 1)["maxItems"] == entity_schema.MAX_RELATIONSHIPS
    assert relationships(2, 0)["maxItems"] == 0 and relationships(2, 0)["items"]["properties"]["statement"] == {"enum": [1]}
    assert relationships(1, 3)["maxItems"] == 0
