"""HTTP client of the source adapters against simulated answers (httpx2.MockTransport), the total timeout against a
host on 127.0.0.1."""
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx2
import pytest

from app.sources import http as source_http
from app.sources.errors import AccessDenied, SourceRejected, SourceUnavailable
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


@pytest.mark.parametrize(("status", "error"), [(404, SourceRejected), (410, SourceRejected), (403, AccessDenied), (401, AccessDenied)])
def test_client_errors_are_permanent(status, error):
    with client(lambda _request: httpx2.Response(status)) as http, pytest.raises(error, match=f"^HTTP {status}$") as raised:
        http.get("https://feed.example/rss", "*/*")

    # Refused or not, a feed fails at once (SourceRejected); only a refusal is an AccessDenied.
    assert isinstance(raised.value, SourceRejected)
    assert isinstance(raised.value, AccessDenied) == (status in (401, 403))


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


def test_an_answer_above_the_limit_is_cut_there_when_asked_so():
    with client(lambda _request: httpx2.Response(200, content=b"x" * 2000)) as http:
        cut = http.fetch("https://bank.example/robots.txt", "text/plain", 1024, cut=True)
        whole = http.fetch("https://bank.example/robots.txt", "text/plain", 2000, cut=True)
        with pytest.raises(SourceRejected, match="^Antwort grösser als 1 KB$"):
            http.fetch("https://bank.example/robots.txt", "text/plain", 1024)

    assert (len(cut.content), cut.cut) == (1024, True)
    assert (len(whole.content), whole.cut) == (2000, False)


def test_unsupported_scheme_is_rejected():
    # The real transport: it refuses the scheme before connecting anywhere.
    with HttpClient("") as http, pytest.raises(SourceRejected, match="^Ungültige URL$"):
        http.get("ftp://feed.example/rss", "*/*")


COMPLETE = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"


@contextmanager
def local_host(*answers: bytes, trickle: bool = True):
    """A host that answers the requests on one connection in turn and then, with `trickle`, keeps sending a byte
    every 50 ms: never silent for the per-operation timeout. For at most 10 s, so a missing total timeout fails a
    test rather than hanging it. Yields the URL and the list of requests served."""
    server = socket.create_server(("127.0.0.1", 0))
    served = []

    def serve():
        connection, _address = server.accept()
        with connection:
            try:
                for answer in answers:
                    served.append(_read_request(connection))
                    connection.sendall(answer)
                until = time.monotonic() + 10
                while trickle and time.monotonic() < until:
                    connection.sendall(b"x")
                    time.sleep(0.05)
            except OSError:
                pass  # the client shut the connection down

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/page", served
    finally:
        thread.join(15)
        server.close()


def _read_request(connection: socket.socket) -> bytes:
    request = b""
    while b"\r\n\r\n" not in request:
        if not (data := connection.recv(4096)):
            raise OSError("closed")
        request += data
    return request


@pytest.mark.parametrize(
    "head",
    [
        b"HTTP/1.1 200 OK\r\nX-Slow: ",
        b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n",
        # Without a length the shut-down connection would end the answer as if it were complete.
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n",
    ],
    ids=["headers", "body", "body without length"],
)
def test_answer_trickling_in_is_cut_off_after_the_total_timeout(monkeypatch, head):
    monkeypatch.setattr(source_http, "TOTAL_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()

    with local_host(head) as (url, _served), HttpClient("") as http:
        with pytest.raises(SourceUnavailable, match="^Keine vollständige Antwort innerhalb von 0.5 s$"):
            http.get(url, "*/*")

    assert time.monotonic() - started < 5


def test_total_timeout_also_cuts_off_a_reused_connection(monkeypatch):
    monkeypatch.setattr(source_http, "TOTAL_TIMEOUT_SECONDS", 0.5)

    with local_host(COMPLETE, b"HTTP/1.1 200 OK\r\nX-Slow: ") as (url, served), HttpClient("") as http:
        assert http.get(url, "*/*").content == b"ok"
        with pytest.raises(SourceUnavailable, match="^Keine vollständige Antwort"):
            http.get(url, "*/*")

    # Both on the one connection the host accepts: the second request opened none of its own.
    assert len(served) == 2


def test_answer_within_the_total_timeout_is_kept():
    with local_host(COMPLETE, trickle=False) as (url, _served), HttpClient("") as http:
        response = http.get(url, "text/plain")

    assert (response.status, response.content, response.content_type) == (200, b"ok", "text/plain")


def test_request_urls_never_reach_the_log(caplog):
    caplog.set_level("DEBUG")

    with client(lambda request: httpx2.Response(200, text="{}")) as http:
        http.get("https://api.eia.gov/v2/x/data/?api_key=SECRET-KEY", "application/json")

    # httpx2 would log the URL with an API key or a Suchanfrage (datamodel.md, rule 13).
    assert "SECRET-KEY" not in caplog.text
