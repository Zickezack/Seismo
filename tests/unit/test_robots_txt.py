"""robots.txt by RFC 9309 with Google's precedence: the longest matching rule wins, an Allow on a tie."""
import pytest

from app.services import robots_txt


def allows(text, url, token="Seismo"):
    return robots_txt.allows(text, token, url)


def test_a_longer_allow_beats_a_wildcard_disallow():
    # The rules of www.channelnewsasia.com: its feed lies under a disallowed /api/.
    text = "User-agent: *\nDisallow: /api/*\nAllow: /api/v1/rss-outbound-feed\n"

    assert allows(text, "https://cna.example/api/v1/rss-outbound-feed?_format=xml")
    assert not allows(text, "https://cna.example/api/v2/search?q=x")


@pytest.mark.parametrize(
    ("path", "allowed"),
    [("/a/b/c", True), ("/a/c", False), ("/a", False), ("/b", True)],
)
def test_the_longest_matching_rule_wins_whatever_their_order(path, allowed):
    for text in ("User-agent: *\nDisallow: /a\nAllow: /a/b\n", "User-agent: *\nAllow: /a/b\nDisallow: /a\n"):
        assert allows(text, f"https://x.example{path}") is allowed


def test_an_allow_wins_a_tie():
    assert allows("User-agent: *\nDisallow: /p\nAllow: /p\n", "https://x.example/p/1")


@pytest.mark.parametrize(
    ("rule", "path", "allowed"),
    [
        ("/*.pdf$", "/files/report.pdf", False),
        ("/*.pdf$", "/files/report.pdf?v=2", True),
        ("/*?traffic_source=", "/news/a.html?traffic_source=rss", False),
        ("/*?traffic_source=", "/news/a.html", True),
        ("/*/rss.xml", "/updates/rss.xml", False),
        ("/fish*", "/fishheads", False),
        ("/fish", "/Fish.asp", True),  # paths compare case-sensitively
    ],
)
def test_wildcards_and_the_end_anchor(rule, path, allowed):
    assert allows(f"User-agent: *\nDisallow: {rule}\n", f"https://x.example{path}") is allowed


def test_percent_encoding_compares_normalised():
    text = "User-agent: *\nDisallow: /caf%C3%A9\n"

    assert not allows(text, "https://x.example/café/menu")
    assert not allows(text, "https://x.example/caf%c3%a9/menu")


def test_our_group_by_its_exact_name_merged_and_else_the_wildcard():
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: SEISMO\nDisallow: /intern/\n\n"
        "User-agent: Seismograph\nDisallow: /alles/\n\n"
        "User-agent: seismo\nDisallow: /archiv/\n"
    )

    # Both groups named for us apply, the wildcard one does not; "Seismograph" is another crawler.
    assert allows(text, "https://x.example/news")
    assert not allows(text, "https://x.example/intern/a") and not allows(text, "https://x.example/archiv/a")
    assert allows(text, "https://x.example/alles/a")
    assert not allows(text, "https://x.example/news", token="OtherBot")


def test_consecutive_user_agents_share_a_group_and_rules_before_any_are_ignored():
    text = "Disallow: /early/\nUser-agent: GPTBot\nUser-agent: *\nDisallow: /both/\nUser-agent: GPTBot\nDisallow: /more/\n"

    assert allows(text, "https://x.example/early/a")
    assert not allows(text, "https://x.example/both/a")
    assert allows(text, "https://x.example/more/a")
    assert not allows(text, "https://x.example/more/a", token="GPTBot")


@pytest.mark.parametrize("text", ["", "User-agent: *\nDisallow:\n", "User-agent: GPTBot\nDisallow: /\n", "nonsense"])
def test_no_rule_for_us_allows_everything(text):
    assert allows(text, "https://x.example/anything")


def test_robots_txt_itself_is_always_allowed():
    assert allows("User-agent: *\nDisallow: /\n", "https://x.example/robots.txt")


@pytest.mark.parametrize(
    ("text", "delay"),
    [
        ("User-agent: *\nCrawl-delay: 5\nUser-agent: Seismo\nCrawl-delay: 30\n", 30),
        ("User-agent: *\nCrawl-delay: 2.5\n", 2.5),
        ("User-agent: *\nCrawl-delay: bald\n", 0),
        ("User-agent: *\nCrawl-delay: -3\n", 0),
        ("Crawl-delay: 9\n", 0),
    ],
)
def test_crawl_delay_of_the_chosen_group(text, delay):
    assert robots_txt.crawl_delay(text, "Seismo") == delay
