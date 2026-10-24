"""Which fetched items become articles, and in what shape (ingestion_service.article_rows); the minimum interval
of a source's requests."""
from datetime import UTC, datetime, timedelta

import pytest

from app.services import ingestion_service
from app.services.source_service import Source
from app.sources.base import FetchedItem

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
SOURCE = Source(
    id=7, name="EZB – Pressemitteilungen", adapter_type="rss", url="https://www.ecb.europa.eu/rss/press.html",
    query=None, is_active=True, fetch_interval_minutes=60, language="en", region="europe", terms_note=None, raw_text_retention_days=30,
    min_request_interval_seconds=None, created_at=NOW, updated_at=NOW, last_fetched_at=None, last_error=None,
    last_error_at=None, consecutive_failures=0,
)


def item(key, published_at=NOW, **fields):
    values = dict(key=key, url=f"https://www.ecb.europa.eu/press/{key}.html", title=f"Titel {key}", published_at=published_at, language=None)
    return FetchedItem(**values | fields)


def rows(*items):
    return ingestion_service.article_rows(SOURCE, items, CUTOFF)


@pytest.mark.parametrize(("platform", "source", "expected"), [(10, None, 10), (10, 60, 60), (10, 5, 10)])
def test_a_source_s_own_minimum_interval_only_raises_the_platform_s(platform, source, expected):
    assert ingestion_service.request_interval(platform, source) == expected


def test_row_copies_source_name_and_url_and_falls_back_to_the_source_language():
    [row] = rows(item("a"))

    assert row == {
        "source_id": 7,
        "source_name": "EZB – Pressemitteilungen",
        "source_url": "https://www.ecb.europa.eu/rss/press.html",
        "item_key": "a",
        "url": "https://www.ecb.europa.eu/press/a.html",
        "canonical_url": "https://www.ecb.europa.eu/press/a.html",
        "title": "Titel a",
        "published_at": NOW,
        "published_at_source": "feed",
        "language": "en",
    }


def test_row_keeps_the_link_and_fetches_it_without_tracking_parameters():
    [row] = rows(item("a", url="https://www.ecb.europa.eu/press/a.html?utm_source=rss#top"))

    assert row["url"] == "https://www.ecb.europa.eu/press/a.html?utm_source=rss#top"
    assert row["canonical_url"] == "https://www.ecb.europa.eu/press/a.html"


def test_where_the_time_came_from_is_the_adapter_s_and_none_without_a_time():
    [gdelt] = ingestion_service.article_rows(SOURCE, [item("a")], CUTOFF, "gdelt")
    [undated] = rows(item("b", published_at=None))

    assert gdelt["published_at_source"] == "gdelt"
    assert (undated["published_at"], undated["published_at_source"]) == (None, None)


def test_language_of_the_item_wins_over_the_source():
    [row] = rows(item("a", language="de"))

    assert row["language"] == "de"


def test_items_older_than_the_cutoff_are_left_to_the_backfill_undated_ones_always_come():
    kept = rows(item("old", published_at=datetime(2026, 10, 16, 11, 59, tzinfo=UTC)), item("edge", published_at=CUTOFF), item("undated", published_at=None))

    assert [row["item_key"] for row in kept] == ["edge", "undated"]


def test_a_date_more_than_a_day_ahead_is_none():
    soon, later = datetime.now(UTC) + timedelta(hours=20), datetime.now(UTC) + timedelta(days=1, hours=1)

    kept, ahead = rows(item("kept", published_at=soon), item("ahead", published_at=later))

    assert (kept["published_at"], kept["published_at_source"]) == (soon, "feed")
    assert (ahead["published_at"], ahead["published_at_source"]) == (None, None)


def test_items_without_usable_link_are_dropped():
    kept = rows(item("ftp", url="ftp://example.org/a"), item("long", url="https://example.org/" + "x" * 2000), item("ok"))

    assert [row["item_key"] for row in kept] == ["ok"]


def test_each_key_once_the_first_occurrence_wins():
    kept = rows(item("a", title="erste"), item("a", title="zweite"))

    assert [(row["item_key"], row["title"]) for row in kept] == [("a", "erste")]


def test_long_keys_are_stored_as_their_hash_and_long_titles_cut():
    long_key = "ü" * 600  # 1200 bytes

    [row] = rows(item(long_key, title="t" * 1500))

    assert row["item_key"].startswith("sha256:") and len(row["item_key"]) == 7 + 64
    assert len(row["title"]) == 1000


def test_missing_title_stays_missing():
    [row] = rows(item("a", title=None))

    assert row["title"] is None
