"""How robots.txt answers apply (fetch_policy_service), without a database."""
import pytest

from app.db.models import RobotsStatus
from app.services.fetch_policy_service import RobotsRules, _host_of, robots_rules
from app.sources.errors import SourceRejected, SourceUnavailable
from app.sources.http import HttpResponse

RULES = """
User-agent: *
Disallow: /intern/
Crawl-delay: 5

User-agent: Seismo
Disallow: /archiv/
Crawl-delay: 30

User-agent: GPTBot
Disallow: /
"""


def answer(status, body=b""):
    return HttpResponse(url="https://bank.example/robots.txt", content=body, status=status)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, RobotsStatus.DISALLOW_ALL), (403, RobotsStatus.DISALLOW_ALL), (404, RobotsStatus.ALLOW_ALL), (410, RobotsStatus.ALLOW_ALL)],
)
def test_status_of_a_robots_txt_without_rules(status, expected):
    assert robots_rules(answer(status)).status == expected


@pytest.mark.parametrize("status", [429, 500, 503])
def test_robots_txt_that_says_nothing_yet_is_asked_again_later(status):
    with pytest.raises(SourceUnavailable, match=f"^robots.txt: HTTP {status}$"):
        robots_rules(answer(status))


def test_our_own_group_wins_over_the_wildcard():
    rules = robots_rules(answer(200, RULES.encode()))

    assert rules.status == RobotsStatus.RULES
    assert not rules.allows("https://bank.example/archiv/2020.html")
    # The wildcard group no longer applies once a group names Seismo; GPTBot's rules never do.
    assert rules.allows("https://bank.example/intern/x.html")
    assert rules.allows("https://bank.example/press/1.html")
    assert rules.crawl_delay() == 30


def test_wildcard_group_and_its_crawl_delay():
    rules = robots_rules(answer(200, b"User-agent: *\nDisallow: /intern/\nCrawl-delay: 5\n"))

    assert not rules.allows("https://bank.example/intern/x.html")
    assert rules.allows("https://bank.example/press/1.html")
    assert rules.crawl_delay() == 5


def test_undecodable_robots_txt_still_parses():
    rules = robots_rules(answer(200, b"User-agent: *\nDisallow: /\xff\xfe/\n"))

    assert rules.allows("https://bank.example/press/1.html")


def test_allow_all_and_disallow_all_have_no_crawl_delay():
    assert RobotsRules(RobotsStatus.ALLOW_ALL).allows("https://x.example/") and RobotsRules(RobotsStatus.ALLOW_ALL).crawl_delay() == 0
    assert not RobotsRules(RobotsStatus.DISALLOW_ALL).allows("https://x.example/")


@pytest.mark.parametrize(
    ("url", "host", "robots_url"),
    [
        ("https://WWW.Bank.Example/a?b=1", "www.bank.example", "https://www.bank.example/robots.txt"),
        ("http://bank.example:8080/a", "bank.example:8080", "http://bank.example:8080/robots.txt"),
    ],
)
def test_host_key_and_robots_url(url, host, robots_url):
    assert _host_of(url) == (host, robots_url)


def test_url_without_host_is_rejected():
    with pytest.raises(SourceRejected):
        _host_of("https:///a")
