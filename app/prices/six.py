"""SIX Swiss Exchange's prices, as its own website shows them (https://www.six-group.com): Swiss stocks, funds and
SIX's indices, delayed 15 minutes, several per request, by their ValorId - ISIN, currency, market ("CH0038863350CHF4").

No key. Not a documented interface: what SIX's website asks for itself, under its terms; its robots.txt allows the
path, so a request goes through the fetch policy like any other - robots.txt asked, the slots kept.
"""
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.prices.base import Missing, Quote, decimal_of
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import SourceRejected

NAME = "six"
LABEL = "SIX"
DELAYED = True
DELAY_LABEL = "verzögert, 15 Minuten"
URL = "https://www.six-group.com/fqs/snap.json"
ZURICH = ZoneInfo("Europe/Zurich")
BATCH = 20
ACCEPT = "application/json"
QUOTE_FIELDS = (
    "ValorId", "ClosingPrice", "PreviousClosingPrice", "OpeningPrice", "DailyHighPrice", "DailyLowPrice", "TotalVolume",
    "LatestTradeDate", "LatestTradeTime",
)
LOOKUP_FIELDS = ("ValorId", "ValorSymbol", "ShortName", "PortalSegment", "ISIN", "TradingBaseCurrency")
# SIX's segments and the kind of Wertpapier each holds: shares, funds and ETFs, exchange traded products, indices.
SEGMENTS = {"EQ": "stock", "FU": "etf", "EP": "etf", "IN": "index"}


def latest(http: PoliteHttp, valor_ids: Sequence[str]) -> tuple[list[Quote], list[Missing]]:
    """The latest price of each ValorId - its latest trade, in Zurich time - with the day's open, high, low and volume
    and the last trading day's close; those it sent none for."""
    rows = _rows(http, {"select": ",".join(QUOTE_FIELDS), "where": "ValorId=" + "|".join(valor_ids)})
    by_id = {row.get("ValorId"): row for row in rows}
    quotes, missing = [], []
    for valor_id in valor_ids:
        row = by_id.get(valor_id) or {}
        price, at = decimal_of(row.get("ClosingPrice")), _time(row.get("LatestTradeDate"), row.get("LatestTradeTime"))
        if price is None or at is None:
            missing.append(Missing(valor_id, "Kein Kurs von SIX: ValorId unbekannt oder ohne Handel"))
            continue
        volume = row.get("TotalVolume")
        quotes.append(Quote(
            valor_id, at, price, decimal_of(row.get("OpeningPrice")), decimal_of(row.get("DailyHighPrice")),
            decimal_of(row.get("DailyLowPrice")), int(volume) if isinstance(volume, int | float) and not isinstance(volume, bool) else None,
            decimal_of(row.get("PreviousClosingPrice")),
        ))
    return quotes, missing


def lookup(http: PoliteHttp, symbol: str) -> list[dict]:
    """The Wertpapiere SIX lists for a symbol: ValorId, symbol, name, kind, ISIN, currency."""
    query = {"select": ",".join(LOOKUP_FIELDS), "where": "PortalSegment=" + "|".join(SEGMENTS), "pagesize": 10, "match": symbol}
    return [
        {"valor_id": row["ValorId"], "symbol": row.get("ValorSymbol") or symbol, "name": row.get("ShortName") or symbol,
         "kind": SEGMENTS.get(row.get("PortalSegment"), "stock"), "isin": row.get("ISIN"), "currency": row.get("TradingBaseCurrency")}
        for row in _rows(http, query) if row.get("ValorId")
    ]


def _rows(http: PoliteHttp, query: dict) -> list[dict]:
    response = http.get(f"{URL}?{urlencode(query)}", ACCEPT)
    try:
        data = json.loads(response.content)
        names, rows = data["colNames"], data["rowData"]
        return [dict(zip(names, row, strict=False)) for row in rows]
    except (ValueError, KeyError, TypeError):
        raise SourceRejected("Antwort von SIX ist keine Kurstabelle") from None


def _time(day: object, clock: object) -> datetime | None:
    """A date as 20261030 and a time as 173644.267, in Zurich: in UTC."""
    if not isinstance(day, int) or not isinstance(clock, int | float) or isinstance(clock, bool):
        return None
    whole = int(clock)
    try:
        local = datetime(day // 10000, day // 100 % 100, day % 100, whole // 10000, whole // 100 % 100, whole % 100,
                         round((clock - whole) * 1_000_000) % 1_000_000, tzinfo=ZURICH)
    except ValueError:
        return None
    return local.astimezone(UTC)
