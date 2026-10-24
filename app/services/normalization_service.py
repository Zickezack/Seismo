"""Normalization: an article's raw text cleaned to plain text, its metadata made consistent - canonical URL, title,
publication time in UTC, detected language (userstories.md, Normalisierung, Deduplizierung und Clustering, story 1).

The worker queues a job for every article whose full text is settled, once a minute (enqueue_due); the job reads
the raw text outside any transaction and writes the result in the transaction that completes it. Without a raw text
only the link is made canonical. The cleaned text is full text too: it stays with the raw text and goes with it
(datamodel.md, rule 8).
"""
import io
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import lxml.html
import trafilatura
from lxml.etree import ParserError
from py3langid.langid import MODEL_DIR, MODEL_FILE, LanguageIdentifier
from pypdf import PdfReader
from sqlalchemy import Connection, Engine

from app.db.models import BACKFILL_PRIORITY, LIVE_PRIORITY, NormalizationStatus, PublishedAtSource
from app.repositories import normalization_repository
from app.services import job_service
from app.services.ingestion_service import MAX_PUBLICATION_AHEAD
from app.services.urls import clean_url
from app.sources import ADAPTERS
from app.sources.gdelt import LANGUAGES as GDELT_LANGUAGES

logger = logging.getLogger(__name__)

# Articles queued per check; the worker checks once a minute.
ENQUEUE_LIMIT = 200
MAX_URL_LENGTH = 2000
MAX_TITLE_LENGTH = 1000
# Detection: from this much text, only when this sure, among the languages GDELT knows - a page that lists every
# EU language would otherwise come out as Guadeloupean Creole. Anything less stays unknown (datamodel.md, rule 5).
MIN_TEXT_FOR_LANGUAGE = 200
MIN_LANGUAGE_CONFIDENCE = 0.6
LANGUAGE_SAMPLE = 5000
# The page's own publication time counts only as a full timestamp with time zone, and within reason.
EARLIEST_PUBLICATION = datetime(1990, 1, 1, tzinfo=UTC)
PUBLISHED_TIME_NAMES = ("article:published_time", "og:article:published_time", "datepublished", "dc.date.issued")
HTML_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(frozen=True)
class Page:
    """What the raw text says: its main text and, for a web page, its own canonical link, title and time."""

    text: str
    canonical_url: str | None = None
    title: str | None = None
    published_at: datetime | None = None


def enqueue_due(engine: Engine) -> list[int]:
    """Queue a normalization for every article whose full text is settled; a backfill's behind live work."""
    with engine.begin() as connection:
        due = normalization_repository.due(connection, job_service.NORMALIZE_JOB_KIND, ENQUEUE_LIMIT)
        job_ids = [
            job_service.enqueue(
                connection,
                job_service.NORMALIZE_JOB_KIND,
                {"article_id": row.id},
                dedupe_key=f"{job_service.NORMALIZE_JOB_KIND}:{row.id}",
                priority=BACKFILL_PRIORITY if row.backfill_id is not None else LIVE_PRIORITY,
            )
            for row in due
        ]
    return [job_id for job_id in job_ids if job_id is not None]


def normalize(engine: Engine, article_id: int) -> Callable[[Connection], None] | None:
    """First phase of a normalization job: read the raw text. Returns the effect that writes the result, or marks the
    article failed when its text cannot be read; None when there is nothing to do any more."""
    with engine.connect() as connection:
        article = normalization_repository.get(connection, article_id)
    if article is None or article.normalization_status != NormalizationStatus.PENDING:
        return None
    link = article.canonical_url or clean_url(article.url)
    try:
        page = None if article.content is None else read_page(article.content, article.content_type, article.raw_url)
    except Exception as error:
        # A raw text no reader understands: retrying would not help.
        failed = {
            "canonical_url": link,
            "normalization_status": NormalizationStatus.FAILED,
            "normalization_error": job_service.describe_error(error),
        }
        return lambda connection: normalization_repository.finish(connection, article_id, failed, None)
    values = {
        "canonical_url": (page and page.canonical_url) or link,
        "normalization_status": NormalizationStatus.DONE,
        "normalization_error": None,
        "detected_language": detect_language(page.text) if page else None,
    }
    # The page's own title fills a gap and replaces one its source states worse (GDELT's come tokenized).
    adapter = ADAPTERS.get(article.adapter_type or "")
    if page and page.title and (not article.title or (adapter and adapter.page_title_preferred)):
        values["title"] = page.title
    # The page's own time fills a gap and replaces GDELT's seendate, which is only when GDELT saw the article.
    replaceable = article.published_at is None or article.published_at_source == PublishedAtSource.GDELT
    if page and page.published_at and replaceable:
        values |= {"published_at": page.published_at, "published_at_source": PublishedAtSource.PAGE}

    def store(connection: Connection) -> None:
        if normalization_repository.finish(connection, article_id, values, page.text if page else None):
            # IDs and counts only (datamodel.md, rule 13).
            text_chars = len(page.text) if page else None
            fields = {"article_id": article_id, "text_chars": text_chars, "language": values["detected_language"]}
            logger.info("Article normalized", extra=fields)

    return store


def read_page(content: bytes, content_type: str | None, url: str) -> Page:
    """The main text of a raw text - HTML or XHTML (with the page's metadata), PDF or plain text - cleaned."""
    media_type, charset = _media_type(content_type)
    if media_type in HTML_TYPES:
        text = trafilatura.extract(content, url=url, include_comments=False, include_tables=True, favor_precision=True)
        text = clean_text(text or "")
        # A page without a main text is no article page (an app shell, an error page): what it says of itself may
        # concern another one - the EC press corner's shell names its home page, which would make different articles
        # duplicates, and a bot check's title ("Just a moment...") would replace GDELT's.
        return Page(text, *page_metadata(content, url)) if text else Page(text)
    if media_type == "application/pdf":
        reader = PdfReader(io.BytesIO(content))
        return Page(clean_text("\n".join(page.extract_text() or "" for page in reader.pages)))
    return Page(clean_text(content.decode(charset or "utf-8", errors="replace")))


def page_metadata(content: bytes, url: str) -> tuple[str | None, str | None, datetime | None]:
    """A web page's own canonical link (in canonical form), its title (og:title, else <title>) and its publication
    time (meta tags, then JSON-LD datePublished, then <time itemprop>): each None when the page does not state it."""
    try:
        tree = lxml.html.fromstring(content)
    except (ParserError, ValueError):
        return None, None, None
    links = (link for link in tree.iter("link") if "canonical" in (link.get("rel") or "").lower().split())
    canonical = next((link.get("href") for link in links), None)
    canonical_url = _canonical(canonical, url)
    metas = {}
    for meta in tree.iter("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().lower()
        if key and meta.get("content") and key not in metas:
            metas[key] = meta.get("content")
    title_element = tree.find(".//title")
    title = metas.get("og:title") or (title_element.text_content() if title_element is not None else None)
    title = " ".join((title or "").split())[:MAX_TITLE_LENGTH] or None
    candidates = [metas.get(name) for name in PUBLISHED_TIME_NAMES]
    candidates += list(_json_ld_dates(tree))
    times = tree.iter("time")
    candidates += [time.get("datetime") for time in times if (time.get("itemprop") or "").lower() == "datepublished"]
    published_at = next((moment for moment in map(parse_timestamp, candidates) if moment), None)
    return canonical_url, title, published_at


def parse_timestamp(raw: object) -> datetime | None:
    """A full ISO 8601 timestamp with time zone, in UTC; None for a date alone, no zone or an unlikely moment."""
    if not isinstance(raw, str) or "T" not in raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    moment = moment.astimezone(UTC)
    if not EARLIEST_PUBLICATION <= moment <= datetime.now(UTC) + MAX_PUBLICATION_AHEAD:
        return None
    return moment


def detect_language(text: str) -> str | None:
    """The text's language as an ISO 639-1 code when there is enough text and the detection is sure enough."""
    if len(text) < MIN_TEXT_FOR_LANGUAGE:
        return None
    language, confidence = _identifier().classify(text[:LANGUAGE_SAMPLE])
    return language if confidence >= MIN_LANGUAGE_CONFIDENCE else None


def clean_text(text: str) -> str:
    """Lines without surrounding blanks, inner runs of blanks as one, at most one empty line between paragraphs."""
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@lru_cache(maxsize=1)
def _identifier() -> LanguageIdentifier:
    identifier = LanguageIdentifier.from_modelpath(MODEL_DIR / MODEL_FILE, norm_probs=True)
    identifier.set_languages(sorted(set(GDELT_LANGUAGES.values()) & set(identifier.labels)))
    return identifier


def _media_type(content_type: str | None) -> tuple[str, str | None]:
    media_type, _, parameters = (content_type or "").partition(";")
    charset = re.search(r"charset=[\"']?([\w.-]+)", parameters, re.IGNORECASE)
    return media_type.strip().lower(), charset.group(1) if charset else None


def _canonical(href: str | None, base_url: str) -> str | None:
    if not href:
        return None
    url = clean_url(urljoin(base_url, href.strip()))
    if urlsplit(url).scheme not in ("http", "https") or len(url) > MAX_URL_LENGTH:
        return None
    return url


def _json_ld_dates(tree: lxml.html.HtmlElement) -> Iterator[str]:
    for script in tree.iter("script"):
        if (script.get("type") or "").strip().lower() != "application/ld+json" or not script.text:
            continue
        try:
            data = json.loads(script.text)
        except ValueError:
            continue
        yield from _date_published(data)


def _date_published(data: object) -> Iterator[str]:
    if isinstance(data, list):
        for entry in data:
            yield from _date_published(entry)
    elif isinstance(data, dict):
        if isinstance(data.get("datePublished"), str):
            yield data["datePublished"]
        yield from _date_published(data.get("@graph"))
