"""GDELT adapter against a sample answer of the DOC 2.0 API (tests/unit/feeds/gdelt.json), without a network."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.sources.base import FetchedItem
from app.sources.errors import SourceRejected
from app.sources.gdelt import ACCEPT, API_URL, GdeltAdapter, request_url, seen_at
from app.sources.http import HttpResponse

SAMPLE = (Path(__file__).parent / "feeds" / "gdelt.json").read_bytes()
NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)


class Source:
    def __init__(self, query='"central bank" sourcelang:german', last_fetched_at=None, url=API_URL):
        self.url, self.query, self.last_fetched_at = url, query, last_fetched_at


class FakeHttp:
    def __init__(self, content=SAMPLE):
        self.content, self.requests = content, []

    def get(self, url, accept):
        self.requests.append((url, accept))
        return HttpResponse(url=url, content=self.content, content_type="application/json")


def parameters(url):
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


def test_articles_with_seendate_as_publication_time_and_iso_language():
    http = FakeHttp()

    items = GdeltAdapter().fetch(Source(), http)

    assert items == [
        FetchedItem(
            key="https://www.handelsblatt.example/finanzen/ezb-zinsen.html",
            url="https://www.handelsblatt.example/finanzen/ezb-zinsen.html",
            title="EZB belässt die Zinsen",
            published_at=datetime(2026, 10, 23, 14, 15, tzinfo=UTC),
            language="de",
        ),
        FetchedItem(
            key="https://news.example/fed", url="https://news.example/fed", title="Fed holds rates",
            published_at=datetime(2026, 10, 23, 13, 30, tzinfo=UTC), language="en",
        ),
        # Unknown language and an unreadable date stay unknown (datamodel.md, rule 5).
        FetchedItem(key="https://kling.example/a", url="https://kling.example/a", title=None, published_at=None, language=None),
    ]
    [(url, accept)] = http.requests
    assert accept == ACCEPT and url.startswith(API_URL + "?")


def test_no_match_is_an_empty_object():
    assert GdeltAdapter().fetch(Source(), FakeHttp(b"{}")) == []


def test_a_query_gdelt_cannot_run_is_rejected_with_its_reason_as_detail():
    with pytest.raises(SourceRejected) as error:
        GdeltAdapter().fetch(Source('"ab"'), FakeHttp(b"The specified phrase is too short.\n"))

    # The admin sees GDELT's reason; the log gets the summary only (app/sources/errors.py).
    assert str(error.value) == "GDELT lehnt die Suchanfrage ab: The specified phrase is too short."
    assert error.value.summary == "GDELT lehnt die Suchanfrage ab"


def test_json_that_is_no_answer_of_the_api_is_rejected():
    with pytest.raises(SourceRejected, match="^Keine Antwort der GDELT DOC API$"):
        GdeltAdapter().fetch(Source(), FakeHttp(b"[1, 2]"))


def test_a_source_without_query_sends_nothing():
    http = FakeHttp()

    with pytest.raises(SourceRejected, match="^Keine Suchanfrage$"):
        GdeltAdapter().fetch(Source(query=None), http)

    assert http.requests == []


def test_request_asks_for_the_newest_articles_with_the_query_as_is():
    asked = parameters(request_url(Source(), NOW))

    assert asked == {
        "query": '"central bank" sourcelang:german', "mode": "ArtList", "format": "json", "maxrecords": "250",
        "sort": "DateDesc", "timespan": "1440min",
    }
    assert "%20" in request_url(Source(), NOW) and "+" not in request_url(Source(), NOW)


@pytest.mark.parametrize(
    ("last_fetched", "timespan"),
    [
        (None, "1440min"),  # first fetch: the last day
        (timedelta(minutes=5), "35min"),  # since the last success, 30 minutes further back
        (timedelta(hours=3, seconds=1), "211min"),  # whole minutes, rounded up
        (timedelta(days=30), "10080min"),  # at most a week; older belongs to the backfill
        (timedelta(hours=-1), "15min"),  # GDELT's shortest window
    ],
)
def test_window_reaches_back_to_the_last_success(last_fetched, timespan):
    source = Source(last_fetched_at=NOW - last_fetched if last_fetched is not None else None)

    assert parameters(request_url(source, NOW))["timespan"] == timespan


def test_an_address_with_parameters_of_its_own_keeps_them():
    url = request_url(Source(url="https://gdelt.example/doc?trans=googtrans"), NOW)

    assert url.startswith("https://gdelt.example/doc?trans=googtrans&query=")


@pytest.mark.parametrize("raw", ["20261023T1415Z", "20261323T141500Z", "2026-10-23T14:15:00Z", None, 20261023])
def test_unreadable_seendate_is_unknown(raw):
    assert seen_at(raw) is None
