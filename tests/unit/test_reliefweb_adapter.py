"""ReliefWeb adapter against a sample answer of the API v2 (tests/unit/feeds/reliefweb.json), without a network."""
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.sources.base import FetchedItem
from app.sources.errors import AccessDenied, FetchDisallowed, SourceRejected
from app.sources.http import HttpResponse
from app.sources.reliefweb import ACCEPT, API_URL, APPNAME_VARIABLE, ReliefWebAdapter, created_at, request_url

SAMPLE = (Path(__file__).parent / "feeds" / "reliefweb.json").read_bytes()
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class Source:
    def __init__(self, query=None, last_fetched_at=None, url=API_URL):
        self.url, self.query, self.last_fetched_at = url, query, last_fetched_at


class FakeHttp:
    def __init__(self, content=SAMPLE, error=None):
        self.content, self.error, self.requests = content, error, []

    def get(self, url, accept, permitted=False):
        self.requests.append((url, accept, permitted))
        if self.error:
            raise self.error
        return HttpResponse(url=url, content=self.content, content_type="application/json")


@pytest.fixture(autouse=True)
def approved_appname(monkeypatch):
    monkeypatch.setenv(APPNAME_VARIABLE, "ost-seismo-k7q2")


def parameters(url):
    return parse_qsl(urlsplit(url).query)


def test_reports_with_their_friendly_link_posting_time_and_first_language():
    http = FakeHttp()

    items = ReliefWebAdapter().fetch(Source(), http)

    assert items == [
        FetchedItem(
            key="reliefweb:4235445",
            url="https://reliefweb.int/report/jamaica/iom-hurricane-melissa-situation-report-19",
            title="IOM Hurricane Melissa Situation Report 19",
            published_at=datetime(2026, 10, 25, 8, 14, 3, tzinfo=UTC),
            language="en",
        ),
        # Without a friendly link the node link, which redirects there; the time converted to UTC.
        FetchedItem(
            key="reliefweb:4235440", url="https://reliefweb.int/node/4235440", title="PAM Mali : note de pays",
            published_at=datetime(2026, 10, 25, 7, 14, 3, tzinfo=UTC), language="fr",
        ),
        # A date without a time zone and a missing language stay unknown (datamodel.md, rule 5).
        FetchedItem(
            key="reliefweb:4235431", url="https://reliefweb.int/node/4235431", title=None, published_at=None,
            language=None,
        ),
    ]
    [(url, accept, permitted)] = http.requests
    assert accept == ACCEPT and url.startswith(API_URL + "?")
    # The approved appname is the publisher's permission: robots.txt of the API host is not asked.
    assert permitted


def test_request_asks_for_reports_posted_since_the_start_newest_first():
    url = request_url(Source(), "ost-seismo-k7q2", NOW - timedelta(hours=24))

    assert parameters(url) == [
        ("appname", "ost-seismo-k7q2"), ("profile", "minimal"), ("slim", "1"), ("limit", "1000"),
        ("sort[]", "date.created:desc"), ("fields[include][]", "url"), ("fields[include][]", "url_alias"),
        ("fields[include][]", "date.created"), ("fields[include][]", "language.code"),
        ("filter[field]", "date.created"), ("filter[value][from]", "2026-10-24T12:00:00+00:00"),
    ]
    # The API reads a "+" in a date as a blank.
    assert "%2B00%3A00" in url and "+" not in url


def test_the_optional_query_goes_as_is():
    url = request_url(Source('country:Sudan AND theme:"Food and Nutrition"'), "app", NOW)

    assert ("query[value]", 'country:Sudan AND theme:"Food and Nutrition"') in parameters(url)
    assert "%20" in url and "+" not in url


@pytest.mark.parametrize(
    ("last_fetched", "since"),
    [(None, NOW - timedelta(hours=24)), (NOW - timedelta(minutes=5), NOW - timedelta(minutes=35))],
)
def test_window_reaches_back_to_the_last_success(last_fetched, since, monkeypatch):
    http = FakeHttp()
    monkeypatch.setattr("app.sources.reliefweb.datetime", _FrozenDatetime)

    ReliefWebAdapter().fetch(Source(last_fetched_at=last_fetched), http)

    assert ("filter[value][from]", since.isoformat()) in parameters(http.requests[0][0])


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_period_request_names_start_and_end_in_utc():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 2, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    http = FakeHttp()

    items = ReliefWebAdapter().fetch_period(Source(), http, start, end)

    asked = parameters(http.requests[0][0])
    assert ("filter[value][from]", "2026-08-01T00:00:00+00:00") in asked
    assert ("filter[value][to]", "2026-08-02T12:30:05+00:00") in asked
    # As ReliefWeb sent them: whether the page was full decides whether older ones may follow.
    assert len(items) == 3


def test_without_appname_nothing_is_sent(monkeypatch):
    monkeypatch.setenv(APPNAME_VARIABLE, " ")
    http = FakeHttp()

    with pytest.raises(SourceRejected, match=r"^Kein ReliefWeb-Appname \(RELIEFWEB_APPNAME\)$"):
        ReliefWebAdapter().fetch(Source(), http)

    assert http.requests == []


def test_a_refusal_of_the_api_names_the_appname():
    with pytest.raises(AccessDenied, match=r"^ReliefWeb: Appname nicht freigegeben \(RELIEFWEB_APPNAME\)$"):
        ReliefWebAdapter().fetch(Source(), FakeHttp(error=AccessDenied("HTTP 403")))


def test_a_bad_request_points_to_the_query():
    with pytest.raises(SourceRejected, match=r"^ReliefWeb lehnt die Anfrage ab \(HTTP 400\)"):
        ReliefWebAdapter().fetch(Source("country:("), FakeHttp(error=SourceRejected("HTTP 400")))


def test_another_host_asks_robots_txt_and_keeps_its_own_errors():
    http = FakeHttp(error=AccessDenied("HTTP 403"))

    with pytest.raises(AccessDenied, match="^HTTP 403$"):
        ReliefWebAdapter().fetch(Source(url="https://mirror.example/v2/reports"), http)

    assert http.requests[0][2] is False
    with pytest.raises(FetchDisallowed):
        ReliefWebAdapter().fetch(Source(), FakeHttp(error=FetchDisallowed("Durch robots.txt gesperrt")))


@pytest.mark.parametrize("content", [b"<html>", b"[1, 2]", b'{"data": {}}'])
def test_anything_but_a_list_of_reports_is_rejected(content):
    with pytest.raises(SourceRejected, match="^Keine Antwort der ReliefWeb API$"):
        ReliefWebAdapter().fetch(Source(), FakeHttp(content))


@pytest.mark.parametrize("raw", ["2026-10-25", "2026-13-25T08:00:00+00:00", None, 20261025])
def test_unreadable_creation_time_is_unknown(raw):
    assert created_at(raw) is None
