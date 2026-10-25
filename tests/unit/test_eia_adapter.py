"""EIA adapter against a sample answer of the Open Data API v2 (tests/unit/feeds/eia.json), without a network."""
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.sources.eia import ACCEPT, KEY_VARIABLE, EiaAdapter, parse_source_url
from app.sources.errors import AccessDenied, SourceRejected
from app.sources.http import HttpResponse

SAMPLE = (Path(__file__).parent / "feeds" / "eia.json").read_bytes()
URL = "https://api.eia.gov/v2/natural-gas/stor/wkly/data/?frequency=weekly&data[0]=value"


class Source:
    def __init__(self, url=URL):
        self.url, self.query, self.last_fetched_at = url, None, None


class FakeHttp:
    def __init__(self, content=SAMPLE, error=None):
        self.content, self.error, self.requests = content, error, []

    def get(self, url, accept, permitted=False):
        self.requests.append((url, accept, permitted))
        if self.error:
            raise self.error
        return HttpResponse(url=url, content=self.content, content_type="application/json")


@pytest.fixture(autouse=True)
def registered_key(monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, "SECRET-KEY")


def test_the_newest_three_periods_become_articles_with_text_written_from_the_numbers():
    http = FakeHttp()

    items = EiaAdapter().fetch(Source(), http)

    assert [item.key for item in items] == [
        "eia:natural-gas/stor/wkly:2026-10-16", "eia:natural-gas/stor/wkly:2026-10-09", "eia:natural-gas/stor/wkly:2026-10-02",
    ]
    newest = items[0]
    assert newest.title == "EIA natural-gas/stor/wkly (weekly): 2026-10-16"
    # EIA states no release time (datamodel.md, rule 5).
    assert (newest.published_at, newest.language) == (None, "en")
    assert newest.text == (
        "U.S. Energy Information Administration (EIA) data release, data set natural-gas/stor/wkly, weekly, period 2026-10-16.\n"
        "Changes are against the previous period 2026-10-09.\n"
        "Weekly East Region Natural Gas Working Underground Storage (Billion Cubic Feet): no value\n"
        "Weekly Lower 48 States Natural Gas Working Underground Storage (Billion Cubic Feet): 3644 BCF (previous 3567, change +77)"
    )
    # A series missing in the period before has no change; decimals keep their places.
    assert items[2].text.splitlines()[2:] == [
        "Weekly Lower 48 States Natural Gas Working Underground Storage (Billion Cubic Feet): 3580.5 BCF (previous 3500, change +80.5)"
    ]
    # The link opens the period in EIA's API browser, without the key.
    assert newest.url.startswith("https://www.eia.gov/opendata/browser/natural-gas/stor/wkly?frequency=weekly&")
    assert newest.url.endswith("start=2026-10-16&end=2026-10-16") and "SECRET" not in newest.url


def test_request_keeps_the_data_set_and_sets_paging_sorting_and_the_key():
    http = FakeHttp()
    source = Source(URL + "&facets[duoarea][]=NUS&start=2020-01-03&sort[0][column]=series&offset=10&length=50")

    EiaAdapter().fetch(source, http)

    [(url, accept, permitted)] = http.requests
    assert url.startswith("https://api.eia.gov/v2/natural-gas/stor/wkly/data/?")
    assert parse_qsl(urlsplit(url).query) == [
        ("frequency", "weekly"), ("data[0]", "value"), ("facets[duoarea][]", "NUS"),
        ("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", "5000"), ("api_key", "SECRET-KEY"),
    ]
    # The registered key is EIA's permission: robots.txt of the API host is not asked.
    assert accept == ACCEPT and permitted


def test_on_a_full_page_the_oldest_period_only_serves_as_comparison(monkeypatch):
    monkeypatch.setattr("app.sources.eia.MAX_LENGTH", 7)
    monkeypatch.setattr("app.sources.eia.LIVE_PERIODS", 10)

    items = EiaAdapter().fetch(Source(), FakeHttp())

    assert [item.key.rsplit(":", 1)[1] for item in items] == ["2026-10-16", "2026-10-09", "2026-10-02", "2026-09-25"]
    assert "change +100" in items[-1].text


def test_without_key_nothing_is_sent(monkeypatch):
    monkeypatch.delenv(KEY_VARIABLE)
    http = FakeHttp()

    with pytest.raises(SourceRejected, match=r"^Kein EIA-API-Key \(EIA_API_KEY\)$"):
        EiaAdapter().fetch(Source(), http)

    assert http.requests == []


def test_a_refused_key_and_a_bad_request_say_what_to_check():
    with pytest.raises(AccessDenied, match=r"^EIA: API-Key ungültig \(EIA_API_KEY\)$"):
        EiaAdapter().fetch(Source(), FakeHttp(error=AccessDenied("HTTP 403")))
    with pytest.raises(SourceRejected, match=r"^EIA lehnt die Anfrage ab \(HTTP 400\): Route und Parameter"):
        EiaAdapter().fetch(Source(), FakeHttp(error=SourceRejected("HTTP 400")))


def test_an_error_in_the_answer_is_shown_as_detail():
    answer = json.dumps({"error": "Invalid frequency 'daily' provided.", "code": 400}).encode()

    with pytest.raises(SourceRejected) as error:
        EiaAdapter().fetch(Source(), FakeHttp(answer))

    assert str(error.value) == "EIA lehnt die Anfrage ab: Invalid frequency 'daily' provided."
    assert error.value.summary == "EIA lehnt die Anfrage ab"


@pytest.mark.parametrize("content", [b"<html>", b"[]", b'{"response": {}}'])
def test_anything_but_rows_is_rejected(content):
    with pytest.raises(SourceRejected, match="^Keine Antwort der EIA API$"):
        EiaAdapter().fetch(Source(), FakeHttp(content))


@pytest.mark.parametrize(
    "url", ["https://api.eia.gov/v2/natural-gas/stor/wkly/", "https://api.eia.gov/natural-gas/data/", "https://www.eia.gov/rss/x.xml"]
)
def test_a_url_that_is_no_data_set_is_rejected(url):
    with pytest.raises(SourceRejected, match="^URL: die API-URL eines Datensatzes"):
        parse_source_url(url)


def test_no_backfill():
    assert EiaAdapter.offers_backfill is False
    with pytest.raises(SourceRejected):
        EiaAdapter().fetch_period(Source(), FakeHttp(), datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
