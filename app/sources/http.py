"""HTTP for the source adapters: one User-Agent, timeouts, a size limit and a few redirects."""
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import version

import httpx2

from app.sources.errors import SourceRejected, SourceUnavailable

TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
# Worth another attempt later; every other 4xx answer stays the same however often it is asked.
RETRYABLE_STATUS = {408, 425, 429}


@dataclass(frozen=True)
class HttpResponse:
    url: str  # after redirects; the base for relative links
    content: bytes
    status: int = 200
    content_type: str | None = None
    retry_after: float | None = None  # seconds, from the Retry-After header


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

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._client.close()

    def get(self, url: str, accept: str) -> HttpResponse:
        """A successful answer; any other raises SourceRejected or SourceUnavailable."""
        response = self.fetch(url, accept)
        if response.status >= 500 or response.status in RETRYABLE_STATUS:
            raise unavailable(response)
        if response.status >= 400:
            raise SourceRejected(f"HTTP {response.status}")
        return response

    def fetch(self, url: str, accept: str, max_bytes: int | None = None) -> HttpResponse:
        """The answer whatever its status; only a failed connection or an answer above `max_bytes` raises."""
        max_bytes = max_bytes or MAX_RESPONSE_BYTES
        try:
            with self._client.stream("GET", url, headers={"Accept": accept}) as response:
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
                )
        except (httpx2.InvalidURL, httpx2.UnsupportedProtocol):
            raise SourceRejected("Ungültige URL") from None
        except httpx2.TooManyRedirects:
            raise SourceRejected(f"Mehr als {MAX_REDIRECTS} Weiterleitungen") from None
        except httpx2.TimeoutException:
            raise SourceUnavailable(f"Keine Antwort innerhalb von {TIMEOUT_SECONDS} s") from None
        except httpx2.TransportError as error:
            raise SourceUnavailable(f"Verbindung fehlgeschlagen: {type(error).__name__}") from None


def unavailable(response: HttpResponse, prefix: str = "") -> SourceUnavailable:
    """The error for an answer worth another attempt later, with the pause the host asked for, if any."""
    message = f"{prefix}HTTP {response.status}"
    if response.retry_after:
        message += f" (Retry-After {round(response.retry_after)} s)"
    return SourceUnavailable(message, response.retry_after)


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
