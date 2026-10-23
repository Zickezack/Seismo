"""RSS adapter on sample feeds (tests/unit/feeds): RSS 2.0, RSS 1.0, Atom, a web page, an empty feed."""
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.sources import ADAPTERS, ADAPTER_LABELS
from app.sources.base import FetchedItem, language_code
from app.sources.errors import SourceRejected
from app.sources.http import HttpResponse
from app.sources.rss import ACCEPT, RssAdapter

FEEDS = Path(__file__).parent / "feeds"


class FakeHttp:
    """Answers every request with one sample file, as if served from `url`."""

    def __init__(self, name, url="https://bank.example/rss/press.xml"):
        self.content = (FEEDS / name).read_bytes()
        self.url = url
        self.requests = []

    def get(self, url, accept):
        self.requests.append((url, accept))
        return HttpResponse(url=self.url, content=self.content)


class Source:
    url = "https://bank.example/rss/press"


def fetch(name, **http):
    return RssAdapter().fetch(Source(), FakeHttp(name, **http))


def test_rss2_items_with_utc_times_feed_language_and_resolved_links():
    items = fetch("rss2.xml")

    assert items == [
        FetchedItem(
            key="press-2026-10-23",
            url="https://bank.example/press/2026-10-23.html",
            title="Zinsentscheid: Leitzins bleibt",
            published_at=datetime(2026, 10, 23, 8, 0, tzinfo=UTC),
            language="de",
        ),
        # No guid: the link is the key. The relative link resolves against the URL the feed came from.
        FetchedItem(
            key="https://bank.example/press/relative.html",
            url="https://bank.example/press/relative.html",
            title="Relativer Link, ohne guid",
            published_at=datetime(2026, 10, 22, 8, 30, tzinfo=UTC),
            language="de",
        ),
        # No date stays no date (datamodel.md, rule 5); items without a web link are left out.
        FetchedItem(
            key="https://bank.example/press/undated.html",
            url="https://bank.example/press/undated.html",
            title="Ohne Datum",
            published_at=None,
            language="de",
        ),
    ]


def test_rss1_takes_dc_date_and_has_no_language():
    [item] = fetch("rss1.xml", url="https://bis.example/rss")

    assert item == FetchedItem(
        key="https://bis.example/speeches/1.htm",
        url="https://bis.example/speeches/1.htm",
        title="Improving cross-border payments",
        published_at=datetime(2026, 10, 21, tzinfo=UTC),
        language=None,
    )


def test_atom_prefers_published_over_updated_and_reads_xml_lang():
    english, french = fetch("atom.xml", url="https://stats.example/feed")

    assert (english.key, english.published_at, english.language) == (
        "tag:stats.example,2026:empsit-10", datetime(2026, 10, 2, 12, 30, tzinfo=UTC), "en"
    )
    assert (french.url, french.published_at, french.language) == (
        "https://stats.example/fr/empsit.htm", datetime(2026, 10, 2, 10, 0, tzinfo=UTC), "fr"
    )


def test_empty_feed_has_no_items():
    assert fetch("empty.xml") == []


def test_web_page_is_rejected():
    with pytest.raises(SourceRejected, match="^Kein RSS- oder Atom-Feed$"):
        fetch("page.html")


def test_asks_for_feeds_at_the_source_url():
    http = FakeHttp("empty.xml")

    RssAdapter().fetch(Source(), http)

    assert http.requests == [("https://bank.example/rss/press", ACCEPT)]


def test_registry_offers_rss():
    assert isinstance(ADAPTERS["rss"], RssAdapter)
    assert ADAPTER_LABELS == {"rss": "RSS", "gdelt": "GDELT"}


@pytest.mark.parametrize(
    ("raw", "code"),
    [("en-US", "en"), ("de_CH", "de"), (" FR ", "fr"), ("gsw", "gsw"), ("", None), (None, None), ("x", None), ("en1", None)],
)
def test_language_code(raw, code):
    assert language_code(raw) == code
