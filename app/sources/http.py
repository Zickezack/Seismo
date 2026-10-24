"""HTTP for the source adapters: one User-Agent, timeouts, a size limit and a few redirects."""
import socket
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import version

import httpx2

from app.sources.errors import AccessDenied, SourceRejected, SourceUnavailable

# Each connect, read and write on its own.
TIMEOUT_SECONDS = 30
# The whole request, redirects included. A host that keeps sending a few bytes in time is never cut off by
# TIMEOUT_SECONDS, and its job would block the worker for good, the lease renewed all the while.
TOTAL_TIMEOUT_SECONDS = 60
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
# Worth another attempt later; every other 4xx answer stays the same however often it is asked.
RETRYABLE_STATUS = {408, 425, 429}
# The host refuses this client, not the resource: often a bot protection (AccessDenied).
REFUSED_STATUS = {401, 403}


@dataclass(frozen=True)
class HttpResponse:
    url: str  # after redirects; the base for relative links
    content: bytes
    status: int = 200
    content_type: str | None = None
    retry_after: float | None = None  # seconds, from the Retry-After header
    headers: Mapping[str, str] = field(default_factory=dict)  # names in lower case


def user_agent(contact: str) -> str:
    """Names the platform and, when configured, how to reach its operator - some sources (BLS) require it."""
    name = f"Seismo/{version('seismo')}"
    return f"{name} (+{contact})" if contact else name


class HttpClient:
    """Use as a context manager; it keeps its connections open until the end of the block."""

    def __init__(self, contact: str, transport: httpx2.BaseTransport | None = None) -> None:
        self._client = httpx2.Client(
            headers={"User-Agent": user_agent(contact)},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            transport=transport,
        )
        self._watchdog = _Watchdog()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._client.close()

    def get(self, url: str, accept: str) -> HttpResponse:
        """A successful answer; any other raises SourceRejected or SourceUnavailable."""
        response = self.fetch(url, accept)
        if response.status >= 500 or response.status in RETRYABLE_STATUS:
            raise unavailable(response)
        if response.status in REFUSED_STATUS:
            raise AccessDenied(f"HTTP {response.status}")
        if response.status >= 400:
            raise SourceRejected(f"HTTP {response.status}")
        return response

    def fetch(self, url: str, accept: str, max_bytes: int | None = None) -> HttpResponse:
        """The answer whatever its status; only a failed connection, an answer above `max_bytes` or one not complete
        within TOTAL_TIMEOUT_SECONDS raises."""
        max_bytes = max_bytes or MAX_RESPONSE_BYTES
        try:
            with self._watchdog.armed(TOTAL_TIMEOUT_SECONDS):
                response = self._receive(url, accept, max_bytes)
        except (httpx2.InvalidURL, httpx2.UnsupportedProtocol):
            raise SourceRejected("Ungültige URL") from None
        except httpx2.TooManyRedirects:
            raise SourceRejected(f"Mehr als {MAX_REDIRECTS} Weiterleitungen") from None
        except httpx2.TimeoutException:
            raise SourceUnavailable(f"Keine Antwort innerhalb von {TIMEOUT_SECONDS} s") from None
        except httpx2.TransportError as error:
            if self._watchdog.fired:
                raise _incomplete() from None
            raise SourceUnavailable(f"Verbindung fehlgeschlagen: {type(error).__name__}") from None
        # An answer without a length ends at the shut-down connection as if it were complete.
        if self._watchdog.fired:
            raise _incomplete()
        return response

    def _receive(self, url: str, accept: str, max_bytes: int) -> HttpResponse:
        extensions = {"trace": self._watchdog.trace}
        with self._client.stream("GET", url, headers={"Accept": accept}, extensions=extensions) as response:
            content = bytearray()
            for chunk in response.iter_bytes():
                content += chunk
                if len(content) > max_bytes:
                    raise SourceRejected(f"Antwort grösser als {_size(max_bytes)}")
            return HttpResponse(
                url=str(response.url),
                content=bytes(content),
                status=response.status_code,
                content_type=response.headers.get("content-type"),
                retry_after=retry_after_seconds(response.headers.get("retry-after")),
                headers={name.lower(): value for name, value in response.headers.items()},
            )


class _Watchdog:
    """Ends a request once its total time is up by shutting down the client's connections: a read blocked on one
    returns at once, where closing the socket would not wake it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Of every connection the client opened: a later request may reuse one without opening it again.
        self._sockets: list[socket.socket] = []
        self.fired = False

    def trace(self, event: str, info: Mapping) -> None:
        """The request's trace extension (httpcore2): remembers each new connection's socket, TLS included."""
        if not event.endswith((".connect_tcp.complete", ".start_tls.complete")):
            return
        sock = info["return_value"].get_extra_info("socket")
        with self._lock:
            self._sockets.append(sock)
            if self.fired:
                _shut_down(sock)

    @contextmanager
    def armed(self, seconds: float) -> Iterator[None]:
        self.fired = False
        timer = threading.Timer(seconds, self._fire)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()
            # A shutdown under way is over before the request's outcome is read.
            timer.join()

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            for sock in self._sockets:
                _shut_down(sock)


def _shut_down(sock: socket.socket) -> None:
    try:
        # The plain socket's shutdown, also under TLS: SSLSocket.shutdown would drop the TLS state of a read
        # running meanwhile in the request's thread.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass  # closed already


def _incomplete() -> SourceUnavailable:
    return SourceUnavailable(f"Keine vollständige Antwort innerhalb von {TOTAL_TIMEOUT_SECONDS} s")


def unavailable(response: HttpResponse, prefix: str = "") -> SourceUnavailable:
    """The error for an answer worth another attempt later, with the pause the host asked for, if any."""
    message = f"{prefix}HTTP {response.status}"
    if response.retry_after:
        message += f" (Retry-After {round(response.retry_after)} s)"
    return SourceUnavailable(message, retry_after=response.retry_after)


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """The seconds a Retry-After header asks to wait - as a number or an HTTP date; None for none or a past date."""
    value = (value or "").strip()
    if value.isdigit():
        seconds = float(value)
    else:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        # A date with "-0000" comes back without a time zone; HTTP dates are always UTC.
        moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        seconds = (moment - (now or datetime.now(UTC))).total_seconds()
    return seconds if seconds > 0 else None


def _size(max_bytes: int) -> str:
    return f"{max_bytes // (1024 * 1024)} MB" if max_bytes >= 1024 * 1024 else f"{max_bytes // 1024} KB"
