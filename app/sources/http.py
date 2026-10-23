"""HTTP for the source adapters: one User-Agent, timeouts, a size limit and a few redirects."""
from dataclasses import dataclass
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
        try:
            with self._client.stream("GET", url, headers={"Accept": accept}) as response:
                if response.status_code >= 500 or response.status_code in RETRYABLE_STATUS:
                    raise SourceUnavailable(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise SourceRejected(f"HTTP {response.status_code}")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content += chunk
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise SourceRejected(f"Antwort grösser als {MAX_RESPONSE_BYTES // (1024 * 1024)} MB")
                return HttpResponse(url=str(response.url), content=bytes(content))
        except (httpx2.InvalidURL, httpx2.UnsupportedProtocol):
            raise SourceRejected("Ungültige URL") from None
        except httpx2.TooManyRedirects:
            raise SourceRejected(f"Mehr als {MAX_REDIRECTS} Weiterleitungen") from None
        except httpx2.TimeoutException:
            raise SourceUnavailable(f"Keine Antwort innerhalb von {TIMEOUT_SECONDS} s") from None
        except httpx2.TransportError as error:
            raise SourceUnavailable(f"Verbindung fehlgeschlagen: {type(error).__name__}") from None
