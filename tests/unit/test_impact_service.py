"""The Wirkungsannahmen's rules without a database (impact_service): the classification, the checks of the LLM's
answer, the schema and the analyst's form."""
import json

import pytest

from app.services import impact_service as impacts
from app.services.impact_service import Context, InvalidHypothesis, Item, Target, UnusableAnswer
from app.services.relationship_service import InEffect

SHELL, TOTAL, EXXON = 1, 2, 3
EFFECT = Item(1, {"candidate_id": 10, "impact": 0}, "impact", "Shell: negative", "margins squeezed", 10, SHELL, "negative")
FORECAST = Item(2, {"statement_id": 7}, "statement", "Rates stay high.", "rates stay high", 10, statement_kind="source_forecast")
COMPETES = InEffect(5, "competes", SHELL, "Shell", "company", EXXON, "ExxonMobil", "company", "unreviewed")
CONTEXT = Context(
    1, "Freight rates squeeze margins", 1, [], [EFFECT, FORECAST],
    [Target(1, SHELL, "Shell", "company", True), Target(2, TOTAL, "TotalEnergies", "company", True),
     Target(3, EXXON, "ExxonMobil", "company", False)],
    [COMPETES], frozenset({SHELL, TOTAL}),
)


def hypothesis(target, path="direct", via=None, **fields):
    values = {"target": target, "path": path, "via": via, "direction": "negative", "horizon": "months", "mechanism": "Costs.",
              "counterarguments": "Hedged.", "support": [1], "against": [], "classification": "evidenced"}
    return values | fields


def found(*items):
    return impacts.proposals(json.dumps({"reason": "", "hypotheses": list(items)}), CONTEXT)


# The rule


def test_belegt_only_when_a_report_names_this_effect_on_this_target_in_this_direction():
    assert impacts.classify("direct", "negative", SHELL, [EFFECT], False) == "evidenced"
    assert impacts.classify("direct", "positive", SHELL, [EFFECT], False) == "plausible"
    assert impacts.classify("direct", "negative", TOTAL, [EFFECT], False) == "plausible"
    # Indirect never belegt: the report names the effect on another.
    assert impacts.classify("indirect", "negative", SHELL, [EFFECT], True) == "plausible"


def test_plausibel_through_a_relationship_or_a_beleg_else_spekulativ():
    assert impacts.classify("indirect", "positive", EXXON, [], True) == "plausible"
    assert impacts.classify("direct", "positive", TOTAL, [FORECAST], False) == "plausible"
    assert impacts.classify("direct", "positive", TOTAL, [], False) == "speculative"


def test_an_effect_without_a_quote_evidences_nothing():
    unquoted = Item(1, EFFECT.ref, "impact", EFFECT.text, None, 10, SHELL, "negative")
    assert impacts.classify("direct", "negative", SHELL, [unquoted], False) == "plausible"


def test_the_proposal_is_capped_never_raised():
    assert impacts.capped("evidenced", impacts.ImpactClass.PLAUSIBLE) == "plausible"
    assert impacts.capped("speculative", impacts.ImpactClass.EVIDENCED) == "speculative"
    assert impacts.capped(None, impacts.ImpactClass.PLAUSIBLE) == "plausible"
    assert impacts.capped("certain", impacts.ImpactClass.SPECULATIVE) == "speculative"


# The answer


def test_the_answer_keeps_what_holds_once_and_counts_what_it_leaves_out():
    proposals, dropped = found(
        hypothesis(1),
        hypothesis(3, "indirect", 1, direction="positive", support=[]),
        hypothesis(3),  # not named: not direct
        hypothesis(2, "indirect", 1),  # not TotalEnergies' relationship
        hypothesis(1, direction="positive"),  # Shell directly again
        hypothesis(4),  # no such target
        hypothesis(2, mechanism="  "),  # no mechanism
        hypothesis(2, horizon="decades"),
        "not an object",
    )
    assert [(each.target_entity_id, each.path, each.via_relationship_id, each.via_entity_id) for each in proposals] == [
        (SHELL, "direct", None, None), (EXXON, "indirect", 5, SHELL),
    ]
    assert dropped == 7
    shell, exxon = proposals
    assert (shell.classification, shell.proposed_classification, shell.support) == ("evidenced", "evidenced", [EFFECT.ref])
    assert (exxon.classification, exxon.proposed_classification) == ("plausible", "evidenced")


def test_cited_numbers_outside_the_belege_are_left_out_each_once():
    [proposal], _ = found(hypothesis(2, support=[2, 2, 9, "1"], against=[1]))
    assert (proposal.support, proposal.against) == ([FORECAST.ref], [EFFECT.ref])


@pytest.mark.parametrize("content", ["", "{}", '{"hypotheses": {}}', "not json"])
def test_an_answer_without_hypotheses_is_unusable(content):
    with pytest.raises(UnusableAnswer):
        impacts.proposals(content, CONTEXT)


def test_the_schema_offers_only_the_numbers_shown():
    item = impacts.schema(CONTEXT)["properties"]["hypotheses"]["items"]["properties"]
    assert item["target"] == {"enum": [1, 2, 3]}
    assert item["via"] == {"enum": [1, None]}
    assert item["support"]["items"] == {"enum": [1, 2]}
    empty = Context(1, "", 0, [], [], CONTEXT.targets, [], CONTEXT.named_ids)
    empty_item = impacts.schema(empty)["properties"]["hypotheses"]["items"]["properties"]
    assert empty_item["support"]["maxItems"] == 0 and empty_item["via"] == {"enum": [None]}


def test_the_question_marks_targets_only_reachable_through_a_relationship():
    content = impacts.messages(CONTEXT)[1]["content"]
    assert "3. ExxonMobil (company) - not named by the event" in content
    assert "1. Shell competes with ExxonMobil" in content
    assert '1. [effect named] Shell: negative - "margins squeezed"' in content


# The form


def test_a_beleg_travels_as_a_form_value_and_back():
    for ref in (EFFECT.ref, FORECAST.ref):
        assert impacts.parse_ref(impacts.ref_key(ref)) == ref
    assert impacts.parse_ref("x1") is None and impacts.parse_ref("i1") is None


def form(**fields):
    return {"target": str(TOTAL), "direction": "negative", "horizon": "weeks", "mechanism": "Costs.", "counterarguments": "Hedged."} | fields


def test_the_form_caps_the_classification_by_the_rule():
    entries = impacts.parse(form(classification="evidenced"), ["s7", "s999"], [], CONTEXT)
    assert entries.fields["classification"] == "plausible"
    assert entries.fields["support"] == [FORECAST.ref]  # one not among the Belege left out
    shell = impacts.parse(form(target=str(SHELL), classification="evidenced"), ["i10.0"], [], CONTEXT)
    assert shell.fields["classification"] == "evidenced"
    exxon = impacts.parse(form(target=str(EXXON), way="5"), [], [], CONTEXT)
    assert (exxon.path, exxon.via_relationship_id, exxon.via_entity_id) == ("indirect", 5, SHELL)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"target": ""}, "Bitte ein Ziel wählen."),
        ({"target": str(EXXON)}, "Direkt nur auf eine Entität"),
        ({"target": str(TOTAL), "way": "5"}, "Indirekt nur über eine Beziehung"),
        ({"direction": "up"}, "Bitte eine Richtung wählen."),
        ({"counterarguments": ""}, "Bitte Gegenargumente nennen"),
        ({"mechanism": "x" * 601}, "höchstens 600 Zeichen"),
    ],
)
def test_the_form_names_what_is_missing_or_wrong(fields, message):
    with pytest.raises(InvalidHypothesis, match=message):
        impacts.parse(form(**fields), [], [], CONTEXT)
