"""RSS 0.9x, 1.0 and 2.0 and Atom feeds, read with feedparser."""
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import feedparser

from app.sources.base import FetchedItem, Http, SourceSettings, language_code
from app.sources.errors import SourceRejected

ACCEPT = (
    "application/rss+xml, application/atom+xml, application/rdf+xml, "
    "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.1"
)


class RssAdapter:
    label = "RSS"
    needs_query = False
    default_url = None
    page_size = None
    backfill_reach = None

    def fetch(self, source: SourceSettings, http: Http) -> list[FetchedItem]:
        response = http.get(source.url, ACCEPT)
        # The final URL resolves relative links. No content type on purpose: some sources (Destatis)
        # send valid RSS as text/html, which would make feedparser read it as a web page.
        feed = feedparser.parse(response.content, response_headers={"content-location": response.url})
        if not feed.version:
            raise SourceRejected("Kein RSS- oder Atom-Feed")
        feed_language = language_code(feed.feed.get("language"))
        items = [_item(entry, feed_language) for entry in feed.entries]
        return [item for item in items if item is not None]

    def fetch_period(self, source: SourceSettings, http: Http, start: datetime, end: datetime) -> list[FetchedItem]:
        # A feed has no history: the items it still holds from the period. Undated ones come with the live fetch.
        items = [item for item in self.fetch(source, http) if item.published_at and start <= item.published_at < end]
        return sorted(items, key=lambda item: item.published_at, reverse=True)


def _item(entry: feedparser.FeedParserDict, feed_language: str | None) -> FetchedItem | None:
    """None for an item without a web link: nothing to fetch, nothing to cite."""
    url = (entry.get("link") or "").strip()
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    title = " ".join((entry.get("title") or "").split())
    return FetchedItem(
        key=(entry.get("id") or "").strip() or url,
        url=url,
        title=title or None,
        # RSS 1.0 states its date (dc:date) as "updated"; it is the item's only date there, as in Atom without "published".
        published_at=_utc(entry.get("published_parsed") or entry.get("updated_parsed")),
        language=language_code(entry.get("title_detail", {}).get("language")) or feed_language,
    )


def _utc(parsed: time.struct_time | None) -> datetime | None:
    # feedparser has converted it to UTC already.
    return datetime(*parsed[:6], tzinfo=UTC) if parsed else None
