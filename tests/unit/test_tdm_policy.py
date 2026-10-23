"""TDM Reservation Protocol and the list of domains without full text (tdm_policy), without a network."""
import json

import pytest

from app.services import tdm_policy
from app.services.tdm_policy import Layer, decide


def rules(*entries):
    return tdm_policy.parse_tdmrep(json.dumps(list(entries)).encode())


def test_the_first_matching_rule_of_the_file_counts():
    file = rules(
        {"location": "/open/*", "tdm-reservation": 0},
        {"location": "/", "tdm-reservation": 1, "tdm-policy": "https://pub.example/policy.json"},
        {"location": "/open/late/*", "tdm-reservation": 1},
    )

    assert tdm_policy.file_layer(file, "https://pub.example/open/late/a") == Layer("tdmrep.json", False, None)
    assert tdm_policy.file_layer(file, "https://pub.example/news/a") == Layer(
        "tdmrep.json", True, "https://pub.example/policy.json"
    )


@pytest.mark.parametrize(
    ("location", "path", "matches"),
    [("/*.pdf$", "/a/b.pdf", True), ("/*.pdf$", "/a/b.pdf?x=1", False), ("/news/", "/news/a", True), ("/news/", "/sport/a", False), ("/News", "/news", False)],
)
def test_locations_use_the_robots_txt_patterns(location, path, matches):
    file = rules({"location": location, "tdm-reservation": 1})

    assert tdm_policy.file_layer(file, f"https://pub.example{path}").reserved is (True if matches else None)


@pytest.mark.parametrize("value", [2, "yes", True, None, 1.5])
def test_any_other_value_than_0_or_1_is_unset(value):
    assert tdm_policy.file_layer(rules({"location": "/", "tdm-reservation": value}), "https://pub.example/a").reserved is None


@pytest.mark.parametrize("content", [b"<html>", b'{"location": "/"}', b"\xff\xfe"])
def test_a_file_that_is_no_tdmrep_is_none(content):
    assert tdm_policy.parse_tdmrep(content) is None


def test_no_file_or_no_matching_rule_is_unset():
    assert tdm_policy.file_layer(None, "https://pub.example/a") == Layer("tdmrep.json")
    assert tdm_policy.file_layer(rules({"location": "/x/", "tdm-reservation": 1}), "https://pub.example/a").reserved is None


def test_header_overrides_the_file_and_meta_overrides_the_header():
    in_file = Layer("tdmrep.json", True, "https://pub.example/policy")
    header = Layer("HTTP-Header", False)
    meta = Layer("HTML-Meta", True)

    assert decide(in_file, header).reserved is False
    verdict = decide(in_file, header, meta)
    assert (verdict.reserved, verdict.via, verdict.policy) == (True, "HTML-Meta", "https://pub.example/policy")
    assert verdict.reason == "Text- und Data-Mining vorbehalten (HTML-Meta); Bedingungen: https://pub.example/policy"


def test_a_missing_value_leaves_the_earlier_one_and_an_unreserved_page_has_no_policy():
    assert decide(Layer("tdmrep.json", True), Layer("HTTP-Header"), Layer("HTML-Meta")).via == "tdmrep.json"
    unreserved = decide(Layer("HTTP-Header", False, "https://pub.example/policy"))
    assert (unreserved.reserved, unreserved.via, unreserved.policy) == (False, "HTTP-Header", None)
    assert decide() == tdm_policy.Verdict(False)


def test_header_values_with_spaces_and_a_policy():
    layer = tdm_policy.header_layer({"tdm-reservation": " 1 ", "tdm-policy": "https://pub.example/p"})

    assert layer == Layer("HTTP-Header", True, "https://pub.example/p")
    assert tdm_policy.header_layer({}) == Layer("HTTP-Header")


def test_meta_tags_of_an_html_page_only():
    page = b'<html><head><META Name="TDM-Reservation" content="1"><meta name="tdm-policy" content="https://p.example/x"></head></html>'

    assert tdm_policy.meta_layer(page, "text/html; charset=utf-8") == Layer("HTML-Meta", True, "https://p.example/x")
    assert tdm_policy.meta_layer(page, "application/pdf") == Layer("HTML-Meta")
    assert tdm_policy.meta_layer(b"<p>no tags</p>", "text/html") == Layer("HTML-Meta")


@pytest.mark.parametrize(
    ("url", "listed"),
    [
        ("https://www.bbc.co.uk/news/a", "bbc.co.uk"),
        ("https://bbc.co.uk/news/a", "bbc.co.uk"),
        ("https://feeds.bbci.co.uk/news/a", None),  # another domain, only its suffix is alike
        ("https://notbbc.co.uk/a", None),
        ("https://WWW.SRF.CH/news", "srf.ch"),
    ],
)
def test_a_listed_domain_covers_its_subdomains(url, listed):
    assert tdm_policy.blocked_domain(url, ["srf.ch", "bbc.co.uk"]) == listed


def test_domain_list_normalised_and_checked():
    assert tdm_policy.parse_domains(" Bbc.co.uk\n*.srf.ch\n.srf.ch\n\nbbc.co.uk ") == ["bbc.co.uk", "srf.ch"]
    with pytest.raises(ValueError, match="keine Domain: https://x.example/a, localhost"):
        tdm_policy.parse_domains("https://x.example/a\nlocalhost\nok.example")
    with pytest.raises(ValueError, match="höchstens 500 Domains"):
        tdm_policy.parse_domains("\n".join(f"d{index}.example" for index in range(501)))


def test_starter_list_is_valid_and_sorted():
    assert tdm_policy.parse_domains("\n".join(tdm_policy.STARTER_BLOCKED_DOMAINS)) == list(tdm_policy.STARTER_BLOCKED_DOMAINS)
