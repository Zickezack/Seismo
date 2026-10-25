"""EIA Open Data API v2 (https://www.eia.gov/opendata/): energy data releases, e.g. the weekly petroleum stocks and
natural gas storage - the route EIA offers for automated access, since its robots.txt excludes the RSS feeds
(userstories.md, Quellenadapter und Ingestion, story 11).

The Quelle's URL is the API URL EIA's API browser shows for a data set (https://www.eia.gov/opendata/browser),
without the key, e.g. `https://api.eia.gov/v2/natural-gas/stor/wkly/data/?frequency=weekly&data[0]=value`; its
route, frequency, data columns and facets are kept, paging and sorting are the adapter's. Every request carries
EIA_API_KEY; the registered key is EIA's permission, so robots.txt of api.eia.gov (HTTP 403, read as "everything
disallowed") is not asked (architecture.md), the minimum interval per host still applies.

The API returns numbers only. Each of the newest periods becomes one article whose text the adapter writes from
them, with each value's change against the period before (FetchedItem.text): the extraction reads it like any
article. A period comes once - a later revision of its numbers is not fetched again. No backfill: the history of a
series belongs to the market data (userstories.md, Historische Markt- und Ereignisdaten).
"""
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from app.config import source_credential
from app.sources.base import FetchedItem, Http, SourceSettings
from app.sources.errors import AccessDenied, SourceRejected

API_HOST = "api.eia.gov"
BROWSER_URL = "https://www.eia.gov/opendata/browser/"
KEY_VARIABLE = "EIA_API_KEY"
ACCEPT = "application/json"
# The most rows one request returns.
MAX_LENGTH = 5000
# The newest periods each fetch turns into articles: a missed release is caught up, a stored one comes only once.
LIVE_PERIODS = 3
# Set by the adapter, never taken from the Quelle's URL.
OWN_PARAMETERS = ("api_key", "start", "end", "sort", "offset", "length")
MAX_DETAIL_LENGTH = 300


class EiaAdapter:
    label = "EIA"
    accepts_query = False
    needs_query = False
    default_url = None
    page_size = None
    offers_backfill = False
    backfill_reach = None
    published_at_source = "feed"  # never used: EIA states no release time
    page_title_preferred = False

    def fetch(self, source: SourceSettings, http: Http) -> list[FetchedItem]:
        route, kept = parse_source_url(source.url)
        url = request_url(source.url, kept, api_key())
        permitted = urlsplit(url).hostname == API_HOST
        try:
            response = http.get(url, ACCEPT, permitted=permitted)
        except AccessDenied:
            if not permitted:
                raise
            # The API's answer to a missing or unknown key.
            raise AccessDenied(f"EIA: API-Key ungültig ({KEY_VARIABLE})") from None
        except SourceRejected as error:
            if error.summary != "HTTP 400":
                raise
            raise SourceRejected("EIA lehnt die Anfrage ab (HTTP 400): Route und Parameter der URL prüfen") from None
        try:
            answer = json.loads(response.content.decode("utf-8", errors="replace"))
        except ValueError:
            raise SourceRejected("Keine Antwort der EIA API") from None
        if isinstance(answer, dict) and answer.get("error"):
            raise SourceRejected("EIA lehnt die Anfrage ab", _detail(answer["error"]))
        body = answer.get("response") if isinstance(answer, dict) else None
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise SourceRejected("Keine Antwort der EIA API")
        rows = [row for row in body["data"] if isinstance(row, dict) and row.get("period") is not None]
        return releases(route, kept, rows, full_page=len(body["data"]) >= MAX_LENGTH)

    def fetch_period(self, source: SourceSettings, http: Http, start: datetime, end: datetime) -> list[FetchedItem]:
        raise SourceRejected("Quellen vom Typ EIA haben keinen Backfill")


def api_key() -> str:
    key = source_credential(KEY_VARIABLE)
    if not key:
        raise SourceRejected(f"Kein EIA-API-Key ({KEY_VARIABLE})")
    return key


def parse_source_url(url: str) -> tuple[str, list[tuple[str, str]]]:
    """The data set's route (`natural-gas/stor/wkly`) and the URL's parameters the adapter keeps."""
    parts = urlsplit(url)
    path = parts.path.strip("/")
    if not path.startswith("v2/") or not path.endswith("/data"):
        raise SourceRejected("URL: die API-URL eines Datensatzes aus dem EIA-Browser angeben (…/v2/<Route>/data/)")
    kept = [
        (name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.split("[")[0].lower() not in OWN_PARAMETERS
    ]
    return path.removeprefix("v2/").removesuffix("/data"), kept


def request_url(url: str, kept: list[tuple[str, str]], key: str) -> str:
    """The newest rows first, as many as one request returns."""
    own = [("sort[0][column]", "period"), ("sort[0][direction]", "desc"), ("length", MAX_LENGTH), ("api_key", key)]
    base = urlsplit(url)._replace(query="", fragment="").geturl()
    return f"{base}?{urlencode(kept + own, quote_via=quote)}"


def releases(route: str, kept: list[tuple[str, str]], rows: list[dict], full_page: bool) -> list[FetchedItem]:
    """One item per period among the newest LIVE_PERIODS, each compared with the period before it.

    Rows come newest first; on a full page the oldest period may be cut, so it only serves as a comparison.
    """
    by_period: dict[str, list[dict]] = {}
    for row in rows:
        by_period.setdefault(str(row["period"]), []).append(row)
    periods = list(by_period)
    complete = periods[:-1] if full_page else periods
    columns = [value for name, value in kept if name.startswith("data[")] or ["value"]
    frequency = next((value for name, value in kept if name == "frequency"), None)
    items = []
    for index, period in enumerate(complete[:LIVE_PERIODS]):
        previous = periods[index + 1] if index + 1 < len(periods) else None
        text = release_text(route, frequency, period, by_period[period], previous, by_period.get(previous, []), columns)
        browser_parameters = kept + [("start", period), ("end", period)]
        items.append(
            FetchedItem(
                key=f"eia:{route}:{period}",
                url=f"{BROWSER_URL}{route}?{urlencode(browser_parameters, quote_via=quote)}",
                title=f"EIA {route}{f' ({frequency})' if frequency else ''}: {period}",
                published_at=None,  # EIA states no release time (datamodel.md, rule 5)
                language="en",
                text=text,
            )
        )
    return items


def release_text(
    route: str,
    frequency: str | None,
    period: str,
    rows: list[dict],
    previous: str | None,
    previous_rows: list[dict],
    columns: list[str],
) -> str:
    """The release in words: one line per series and data column, with the change against the previous period."""
    heading = f"U.S. Energy Information Administration (EIA) data release, data set {route}"
    heading += f", {frequency}" if frequency else ""
    lines = [f"{heading}, period {period}."]
    lines.append(f"Changes are against the previous period {previous}." if previous else "No previous period.")
    before = {_identity(row, columns): row for row in previous_rows}
    for row in sorted(rows, key=lambda each: _label(each, columns)):
        earlier = before.get(_identity(row, columns))
        for column in columns:
            name = _label(row, columns) + (f" - {column}" if len(columns) > 1 else "")
            lines.append(f"{name}: {_value_line(row, earlier, column)}")
    return "\n".join(lines)


def _identity(row: dict, columns: list[str]) -> tuple:
    """What tells one series from another: every field but the period, the values and their units."""
    skipped = {"period", *columns, *(f"{column}-units" for column in columns), "units"}
    return tuple(sorted((name, str(value)) for name, value in row.items() if name not in skipped))


def _label(row: dict, columns: list[str]) -> str:
    if row.get("series-description"):
        return " ".join(str(row["series-description"]).split())
    names = [str(value) for name, value in sorted(row.items()) if name.endswith(("-name", "Description")) and value]
    return ", ".join(names) or ", ".join(value for _name, value in _identity(row, columns))


def _value_line(row: dict, earlier: dict | None, column: str) -> str:
    value = row.get(column)
    if value is None:
        return "no value"
    units = row.get(f"{column}-units") or row.get("units") or ""
    line = f"{value} {units}".strip()
    if earlier is None or earlier.get(column) is None:
        return line
    change = _number(value) - _number(earlier[column]) if None not in (_number(value), _number(earlier[column])) else None
    if change is None:
        return f"{line} (previous {earlier[column]})"
    return f"{line} (previous {earlier[column]}, change {change:+})"


def _number(raw: object) -> Decimal | None:
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _detail(error: object) -> str | None:
    text = " ".join(str(error).split())
    return text[:MAX_DETAIL_LENGTH] or None
