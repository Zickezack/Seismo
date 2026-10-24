"""The interface every source adapter implements, and the one shape all of them return."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.sources.http import HttpResponse

_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}")


@dataclass(frozen=True)
class FetchedItem:
    """One item of a source as its adapter read it; the ingestion turns it into an Artikel."""

    key: str  # stable within the source: the item's own id, else its link
    url: str
    title: str | None
    published_at: datetime | None  # UTC, as the source states it; None when it states none (datamodel.md, rule 5)
    language: str | None  # ISO 639 code, as the source states it for the item or the whole source


class SourceSettings(Protocol):
    """What an adapter reads of a Quelle."""

    url: str
    query: str | None  # set only for an adapter that needs one
    last_fetched_at: datetime | None  # the last successful fetch, e.g. to ask an API for what is new since


class Http(Protocol):
    """How an adapter sends a request: HttpClient, or the fetch policy around it (fetch_policy_service.PoliteHttp)."""

    def get(self, url: str, accept: str) -> HttpResponse: ...


class SourceAdapter(Protocol):
    label: str  # the source type as the UI shows it, e.g. "RSS"
    needs_query: bool  # a Quelle of this type has a Suchanfrage; the others have none
    default_url: str | None  # the URL a Quelle of this type gets when the admin leaves it empty
    page_size: int | None  # the most items one request returns; None: one request returns all there is (a feed)
    backfill_reach: timedelta | None  # how far back a backfill can go; None: as far as the source holds items
    published_at_source: str  # what its items' published_at is: "feed" (as stated) or "gdelt" (when GDELT saw it)
    page_title_preferred: bool  # the page's own title replaces its items' (normalization_service)

    def fetch(self, source: SourceSettings, http: Http) -> list[FetchedItem]:
        """The source's current items. Raises SourceRejected or SourceUnavailable (app/sources/errors.py)."""

    def fetch_period(self, source: SourceSettings, http: Http, start: datetime, end: datetime) -> list[FetchedItem]:
        """For a backfill: items published from `start` to `end`, newest first, at most `page_size`. Raises as fetch."""


def language_code(raw: str | None) -> str | None:
    """The primary language subtag of a language tag ("en-US" -> "en"), or None for anything else."""
    code = (raw or "").strip().lower().replace("_", "-").split("-")[0]
    return code if _LANGUAGE_CODE.fullmatch(code) else None
