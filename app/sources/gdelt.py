"""GDELT DOC 2.0 API (https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/): the articles GDELT has seen
worldwide that match a Quelle's Suchanfrage, in GDELT's syntax - e.g. `"central bank" sourcelang:german`.

One request per fetch (architecture.md): the newest articles since the last successful fetch, at most
MAX_RECORDS. GDELT asks for at most one request every 5 seconds; the minimum interval per host keeps it.
"""
import json
import math
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

from app.sources.base import FetchedItem, Http, SourceSettings
from app.sources.errors import SourceRejected

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
ACCEPT = "application/json"
# The most one request returns; a Suchanfrage that matches more within one window loses the oldest of them.
MAX_RECORDS = 250
# GDELT publishes what it saw every 15 minutes: ask a little further back than the last fetch. An article that
# comes again is stored only once.
OVERLAP = timedelta(minutes=30)
FIRST_WINDOW = timedelta(hours=24)
# GDELT's shortest window; the longest is the live one, anything older belongs to the backfill (story 6).
MIN_WINDOW = timedelta(minutes=15)
MAX_WINDOW = timedelta(days=7)
MAX_DETAIL_LENGTH = 300
# Exactly this shape: strptime alone would read a shortened "T1415Z" as 14:01:05.
_SEENDATE = re.compile(r"\d{8}T\d{6}Z")
# GDELT names an article's language in English; anything not listed stays unknown (datamodel.md, rule 5).
LANGUAGES = {
    "afrikaans": "af", "albanian": "sq", "arabic": "ar", "armenian": "hy", "azerbaijani": "az", "basque": "eu",
    "belarusian": "be", "bengali": "bn", "bosnian": "bs", "bulgarian": "bg", "catalan": "ca", "chinese": "zh",
    "croatian": "hr", "czech": "cs", "danish": "da", "dutch": "nl", "english": "en", "estonian": "et",
    "filipino": "tl", "finnish": "fi", "french": "fr", "galician": "gl", "georgian": "ka", "german": "de",
    "greek": "el", "gujarati": "gu", "hebrew": "he", "hindi": "hi", "hungarian": "hu", "icelandic": "is",
    "indonesian": "id", "italian": "it", "japanese": "ja", "kannada": "kn", "kazakh": "kk", "korean": "ko",
    "latvian": "lv", "lithuanian": "lt", "macedonian": "mk", "malay": "ms", "malayalam": "ml", "marathi": "mr",
    "mongolian": "mn", "nepali": "ne", "norwegian": "no", "persian": "fa", "polish": "pl", "portuguese": "pt",
    "punjabi": "pa", "romanian": "ro", "russian": "ru", "serbian": "sr", "sinhala": "si", "sinhalese": "si",
    "slovak": "sk", "slovenian": "sl", "somali": "so", "spanish": "es", "swahili": "sw", "swedish": "sv",
    "tagalog": "tl", "tamil": "ta", "telugu": "te", "thai": "th", "tibetan": "bo", "turkish": "tr",
    "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi",
}


class GdeltAdapter:
    label = "GDELT"
    needs_query = True
    default_url = API_URL

    def fetch(self, source: SourceSettings, http: Http) -> list[FetchedItem]:
        if not source.query:
            raise SourceRejected("Keine Suchanfrage")
        response = http.get(request_url(source, datetime.now(UTC)), ACCEPT)
        try:
            # strict=False: titles may hold control characters.
            answer = json.loads(response.content.decode("utf-8", errors="replace"), strict=False)
        except ValueError:
            # A Suchanfrage GDELT cannot run comes back as text saying why, e.g. "The specified phrase is too short."
            raise SourceRejected("GDELT lehnt die Suchanfrage ab", _detail(response.content)) from None
        if not isinstance(answer, dict):
            raise SourceRejected("Keine Antwort der GDELT DOC API")
        # No match at all: an empty object.
        articles = answer.get("articles") or []
        items = [_item(article) for article in articles if isinstance(article, dict)]
        return [item for item in items if item is not None]


def request_url(source: SourceSettings, now: datetime) -> str:
    """The API request for the articles GDELT saw since the last successful fetch, newest first."""
    since = source.last_fetched_at - OVERLAP if source.last_fetched_at else now - FIRST_WINDOW
    window = min(max(now - since, MIN_WINDOW), MAX_WINDOW)
    parameters = {
        "query": source.query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "DateDesc",
        "timespan": f"{math.ceil(window.total_seconds() / 60)}min",
    }
    separator = "&" if urlsplit(source.url).query else "?"
    return f"{source.url}{separator}{urlencode(parameters, quote_via=quote)}"


def seen_at(raw: object) -> datetime | None:
    """GDELT's seendate ("20261023T143000Z"): when it first saw the article, in UTC. Stored as its publication
    time - the admin's decision (2026-10-23); GDELT states no other."""
    if not isinstance(raw, str) or not _SEENDATE.fullmatch(raw):
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:  # e.g. a 13th month
        return None


def _item(article: dict) -> FetchedItem | None:
    """None for an article without a web link."""
    url = str(article.get("url") or "").strip()
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    title = " ".join(str(article.get("title") or "").split())
    return FetchedItem(
        key=url,
        url=url,
        title=title or None,
        published_at=seen_at(article.get("seendate")),
        language=LANGUAGES.get(str(article.get("language") or "").strip().lower()),
    )


def _detail(content: bytes) -> str | None:
    text = " ".join(content.decode("utf-8", errors="replace").split())
    return text[:MAX_DETAIL_LENGTH] or None
