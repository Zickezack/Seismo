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
    values = {"target": str(TOTAL), "direction": "negative", "horizon": "weeks", "mechanism": "Costs.", "counterarguments": "Hedged.",
              "probability": "high"}
    return values | fields


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


# The probability (story 2)


def cap(proposed="high", abstention=None, classification="plausible", support=1, against=0, relationship=False, priced_in=None, expected="no"):
    return impacts.cap_probability(proposed, abstention, classification, support, against, relationship, priced_in, expected)


def test_without_any_beleg_or_relationship_it_is_an_abstention_for_too_little_data():
    assert cap(support=0) == (None, impacts.TOO_LITTLE_DATA)
    assert cap(support=0, relationship=True) == ("high", None)


def test_the_llm_may_abstain_with_its_reason():
    assert cap(proposed=None, abstention="Nothing known of its exposure.") == (None, "Nothing known of its exposure.")
    assert cap(proposed="certain") == (None, "Enthaltung ohne Begründung.")


def test_speculative_or_more_against_than_for_is_low_at_most():
    assert cap(classification="speculative") == ("low", None)
    assert cap(support=1, against=2) == ("low", None)
    assert cap(support=2, against=2) == ("high", None)


def test_priced_in_or_expected_takes_it_one_step_lower_never_below_low():
    assert cap(priced_in=True) == ("medium", None)
    assert cap(expected="yes") == ("medium", None)
    assert cap(proposed="medium", priced_in=True, expected="yes") == ("low", None)
    assert cap(proposed="low", priced_in=True) == ("low", None)
    assert cap(expected="unknown", priced_in=False) == ("high", None)


class Move:
    def __init__(self, abnormal, t):
        self.instrument_id, self.symbol, self.abnormal, self.t = 9, "SHEL", abnormal, t


@pytest.mark.parametrize(
    ("direction", "abnormal", "t", "priced_in"),
    [
        ("negative", -0.05, -2.4, True),
        ("negative", -0.05, -1.5, False),  # not marked enough
        ("positive", -0.05, -2.4, False),  # the other way
        ("positive", 0.04, 2.0, True),
        ("mixed", 0.04, 3.0, None),  # no way to have moved
    ],
)
def test_the_vorlauf_counts_as_priced_in_from_t_2_the_assumed_way(direction, abnormal, t, priced_in):
    assert impacts.is_priced_in(direction, Move(abnormal, t)) is priced_in


def test_an_unmeasured_vorlauf_says_nothing():
    assert impacts.is_priced_in("negative", None) is None
    assert impacts.is_priced_in("negative", Move(None, None)) is None
    assert impacts.market_of("negative", None) == impacts.Market()


def test_the_answer_carries_probability_abstention_and_what_was_expected():
    [shell, total], _ = found(
        hypothesis(1, probability="high", abstention="", expected="yes", expected_support=[2]),
        hypothesis(2, support=[], probability=None, abstention="No exposure known.", expected="unknown", expected_support=[]),
    )
    assert shell.fields()["probability"] == "medium"  # expected: one step lower
    assert (shell.proposed_probability, shell.expected, shell.expected_support) == ("high", "yes", [FORECAST.ref])
    # No Beleg and no relationship: the rule's reason before the LLM's.
    assert (total.fields()["probability"], total.fields()["abstention"]) == (None, impacts.TOO_LITTLE_DATA)


def test_the_schema_asks_for_probability_or_abstention():
    item = impacts.schema(CONTEXT)["properties"]["hypotheses"]["items"]
    assert item["properties"]["probability"] == {"enum": ["high", "medium", "low", None]}
    assert {"probability", "abstention", "expected", "expected_support"} <= set(item["required"])


def test_the_form_needs_a_probability_or_a_reason_to_abstain():
    with pytest.raises(InvalidHypothesis, match="Bitte eine Wahrscheinlichkeit wählen"):
        impacts.parse(form(probability=""), [], [], CONTEXT)
    with pytest.raises(InvalidHypothesis, match="Bitte begründen"):
        impacts.parse(form(probability="abstain"), ["s7"], [], CONTEXT)
    abstained = impacts.parse(form(probability="abstain", abstention="Unclear exposure."), ["s7"], [], CONTEXT)
    assert (abstained.fields["probability"], abstained.fields["abstention"]) == (None, "Unclear exposure.")
    priced = impacts.parse(form(target=str(SHELL)), ["i10.0"], [], CONTEXT, moves={SHELL: Move(-0.06, -3.1)})
    assert (priced.fields["probability"], priced.fields["priced_in"], priced.fields["pre_t"]) == ("medium", True, -3.1)


def test_a_likelihood_reads_as_the_pages_show_it():
    lowered = impacts.Likelihood("medium", "high", None, "yes", [], "SHEL", -0.0612, -3.14, True)
    assert (lowered.label, lowered.lowered_from, lowered.expected_label) == ("mittel", "hoch", "erwartet")
    assert lowered.pre_label == "Vorlauf SHEL: -6,12 % abnormal, t -3,14"
    assert impacts.Likelihood(None, "high", impacts.TOO_LITTLE_DATA).label == "Enthaltung"
    old = impacts.Likelihood()
    assert (old.label, old.assessed) == ("nicht bewertet", False)
