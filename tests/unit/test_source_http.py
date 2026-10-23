"""HTTP client of the source adapters against simulated answers (httpx2.MockTransport)."""
from datetime import UTC, datetime

import httpx2
import pytest

from app.sources import http as source_http
from app.sources.errors import SourceRejected, SourceUnavailable
from app.sources.http import HttpClient, retry_after_seconds, user_agent


def client(handler):
    return HttpClient("ops@example.org", transport=httpx2.MockTransport(handler))


def test_sends_user_agent_with_contact_and_accept():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx2.Response(200, content=b"<rss/>")

    with client(handler) as http:
        response = http.get("https://feed.example/rss", "application/rss+xml")

    assert response.content == b"<rss/>"
    assert seen["user-agent"].startswith("Seismo/") and seen["user-agent"].endswith(" (+ops@example.org)")
    assert seen["accept"] == "application/rss+xml"


def test_user_agent_without_contact():
    assert user_agent("") == f"Seismo/{source_http.version('seismo')}"


def test_follows_redirects_and_reports_the_final_url():
    def handler(request):
        if request.url.path == "/old":
            return httpx2.Response(301, headers={"location": "https://feed.example/new"})
        return httpx2.Response(200, content=b"<rss/>")

    with client(handler) as http:
        assert http.get("https://feed.example/old", "*/*").url == "https://feed.example/new"


def test_too_many_redirects_are_rejected():
    def handler(request):
        return httpx2.Response(302, headers={"location": f"https://feed.example/{len(request.url.path)}x"})

    with client(handler) as http, pytest.raises(SourceRejected, match="^Mehr als 5 Weiterleitungen$"):
        http.get("https://feed.example/start", "*/*")


@pytest.mark.parametrize(("status", "error"), [(404, SourceRejected), (410, SourceRejected), (403, SourceRejected)])
def test_client_errors_are_permanent(status, error):
    with client(lambda _request: httpx2.Response(status)) as http, pytest.raises(error, match=f"^HTTP {status}$"):
        http.get("https://feed.example/rss", "*/*")


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_server_errors_and_rate_limits_are_retried(status):
    with client(lambda _request: httpx2.Response(status)) as http, pytest.raises(SourceUnavailable, match=f"^HTTP {status}$") as error:
        http.get("https://feed.example/rss", "*/*")

    assert error.value.retry_after is None


@pytest.mark.parametrize("status", [429, 503])
def test_retried_answer_carries_the_pause_the_host_asked_for(status):
    answer = httpx2.Response(status, headers={"retry-after": "120"})

    with client(lambda _request: answer) as http, pytest.raises(SourceUnavailable) as error:
        http.get("https://feed.example/rss", "*/*")

    assert (str(error.value), error.value.retry_after) == (f"HTTP {status} (Retry-After 120 s)", 120)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("120", 120),
        (" 30 ", 30),
        ("Fri, 23 Oct 2026 12:05:00 GMT", 300),
        ("Fri, 23 Oct 2026 12:05:00 -0000", 300),
        ("Fri, 23 Oct 2026 11:55:00 GMT", None),
        ("0", None),
        ("-5", None),
        ("1.5", None),
        ("bald", None),
        ("", None),
        (None, None),
    ],
)
def test_retry_after_as_seconds_or_as_a_date(header, seconds):
    assert retry_after_seconds(header, now=datetime(2026, 10, 23, 12, 0, tzinfo=UTC)) == seconds


def test_timeout_and_connection_errors_are_retried():
    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx2.ConnectError("refused", request=request)

    with client(timeout) as http, pytest.raises(SourceUnavailable, match="^Keine Antwort innerhalb von 30 s$"):
        http.get("https://feed.example/rss", "*/*")
    with client(refused) as http, pytest.raises(SourceUnavailable, match="^Verbindung fehlgeschlagen: ConnectError$"):
        http.get("https://feed.example/rss", "*/*")


def test_answer_above_the_size_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(source_http, "MAX_RESPONSE_BYTES", 1024)

    with client(lambda _request: httpx2.Response(200, content=b"x" * 1025)) as http, pytest.raises(SourceRejected):
        http.get("https://feed.example/rss", "*/*")


def test_unsupported_scheme_is_rejected():
    # The real transport: it refuses the scheme before connecting anywhere.
    with HttpClient("") as http, pytest.raises(SourceRejected, match="^Ungültige URL$"):
        http.get("ftp://feed.example/rss", "*/*")
