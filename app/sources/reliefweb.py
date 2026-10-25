"""ReliefWeb API v2 (https://apidoc.reliefweb.int/): humanitarian and conflict reports, the route ReliefWeb offers
for automated access - its robots.txt excludes the RSS feeds (userstories.md, Quellenadapter und Ingestion, story 10).

Every request carries an appname that ReliefWeb approved by e-mail (RELIEFWEB_APPNAME). The approval is the
publisher's permission, so robots.txt of the API host - it disallows every path - is not asked (architecture.md);
the minimum interval per host still applies. ReliefWeb allows 1000 reports per request and 10000 requests a day.

The Suchanfrage is optional, in ReliefWeb's query syntax, e.g. `country:Sudan AND theme:"Food and Nutrition"`;
without one every new report comes. The full text is fetched from the report page as for any article.
"""
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

from app.config import source_credential
from app.sources.base import FetchedItem, Http, SourceSettings, language_code
from app.sources.errors import AccessDenied, SourceRejected

API_URL = "https://api.reliefweb.int/v2/reports"
API_HOST = "api.reliefweb.int"
APPNAME_VARIABLE = "RELIEFWEB_APPNAME"
ACCEPT = "application/json"
MAX_LIMIT = 1000
# A report is listed by when ReliefWeb posted it (date.created, to the second); ask a little further back than the
# last fetch. A report that comes again is stored only once.
OVERLAP = timedelta(minutes=30)
FIRST_WINDOW = timedelta(hours=24)
FIELDS = ("url", "url_alias", "date.created", "language.code")


class ReliefWebAdapter:
    label = "ReliefWeb"
    accepts_query = True
    needs_query = False
    default_url = API_URL
    page_size = MAX_LIMIT
    offers_backfill = True
    backfill_reach = None  # reports since 1996
    published_at_source = "feed"  # date.created: when ReliefWeb published it
    page_title_preferred = False

    def fetch(self, source: SourceSettings, http: Http) -> list[FetchedItem]:
        now = datetime.now(UTC)
        since = source.last_fetched_at - OVERLAP if source.last_fetched_at else now - FIRST_WINDOW
        return self._reports(source, http, request_url(source, appname(), since))

    def fetch_period(self, source: SourceSettings, http: Http, start: datetime, end: datetime) -> list[FetchedItem]:
        return self._reports(source, http, request_url(source, appname(), start, end))

    def _reports(self, source: SourceSettings, http: Http, url: str) -> list[FetchedItem]:
        # Only the approved API: a Quelle pointed elsewhere asks robots.txt like any other.
        permitted = urlsplit(url).hostname == API_HOST
        try:
            response = http.get(url, ACCEPT, permitted=permitted)
        except AccessDenied:
            if not permitted:
                raise
            # The API's answer to an appname it has not approved.
            raise AccessDenied(f"ReliefWeb: Appname nicht freigegeben ({APPNAME_VARIABLE})") from None
        except SourceRejected as error:
            if error.summary != "HTTP 400":
                raise
            raise SourceRejected("ReliefWeb lehnt die Anfrage ab (HTTP 400), z. B. eine ungültige Suchanfrage") from None
        try:
            answer = json.loads(response.content.decode("utf-8", errors="replace"))
        except ValueError:
            raise SourceRejected("Keine Antwort der ReliefWeb API") from None
        if not isinstance(answer, dict) or not isinstance(answer.get("data"), list):
            raise SourceRejected("Keine Antwort der ReliefWeb API")
        items = [_item(report) for report in answer["data"] if isinstance(report, dict)]
        return [item for item in items if item is not None]


def appname() -> str:
    name = source_credential(APPNAME_VARIABLE)
    if not name:
        raise SourceRejected(f"Kein ReliefWeb-Appname ({APPNAME_VARIABLE})")
    return name


def request_url(source: SourceSettings, app: str, start: datetime, end: datetime | None = None) -> str:
    """The API request for the reports ReliefWeb posted from `start` (to `end`), newest first, as GET parameters
    in the bracket form the API reads (`filter[value][from]=...`)."""
    created = {"filter[field]": "date.created", "filter[value][from]": _iso(start)}
    if end is not None:
        created["filter[value][to]"] = _iso(end)
    parameters = [
        ("appname", app), ("profile", "minimal"), ("slim", 1), ("limit", MAX_LIMIT), ("sort[]", "date.created:desc"),
        *(("fields[include][]", name) for name in FIELDS), *created.items(),
    ]
    if source.query:
        parameters.append(("query[value]", source.query))
    separator = "&" if urlsplit(source.url).query else "?"
    # quote, not quote_plus: the API reads a "+" in a date as a blank.
    return f"{source.url}{separator}{urlencode(parameters, quote_via=quote)}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def created_at(raw: object) -> datetime | None:
    """date.created ("2026-10-25T08:14:03+00:00") in UTC; None for anything without a time zone."""
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else None


def _item(report: dict) -> FetchedItem | None:
    """None for a report without an id or a web link."""
    fields = report.get("fields")
    if not isinstance(fields, dict) or report.get("id") is None:
        return None
    # The friendly link is the one the page names as canonical; the node link redirects there.
    url = str(fields.get("url_alias") or fields.get("url") or "").strip()
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    title = " ".join(str(fields.get("title") or "").split())
    date = fields.get("date")
    languages = fields.get("language")
    first_language = languages[0] if isinstance(languages, list) and languages else None
    return FetchedItem(
        key=f"reliefweb:{report['id']}",
        url=url,
        title=title or None,
        published_at=created_at(date.get("created")) if isinstance(date, dict) else None,
        language=language_code(first_language.get("code")) if isinstance(first_language, dict) else None,
    )
