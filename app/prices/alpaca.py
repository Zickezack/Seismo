"""Alpaca's market data (https://docs.alpaca.markets): every US stock and ETF - the active ones and those no longer
traded -, their daily prices since 2016, unadjusted, the corporate actions apart, the exchanges' calendar, and the
latest price. Free with an account: the daily prices of the whole market (SIP), the latest price from IEX; the last
15 minutes are not given.

The keys come from the environment (ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY), in headers, never in a URL or the log.
The keys registered for this application are Alpaca's permission: requests go with `permitted`, only to its API
hosts - the fetch policy's slots and pauses still apply (architecture.md).
"""
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.config import source_credential
from app.prices.base import Action, Asset, Bar, Missing, Quote, Session, decimal_of
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import SourceRejected

NAME = "alpaca"
LABEL = "Alpaca"
KEY_VARIABLES = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
TRADING_HOST = "paper-api.alpaca.markets"
DATA_HOST = "data.alpaca.markets"
DELAYED = False  # IEX's last trade, as it happens; the daily history of all exchanges
DELAY_LABEL = "Echtzeit (IEX)"
NEW_YORK = ZoneInfo("America/New_York")
# The exchanges whose Wertpapiere Seismo keeps; OTC ones are left out.
EXCHANGES = ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS")
# Symbols per request; the URL stays short enough.
BAR_SYMBOLS = 200
BATCH = 200  # Wertpapiere per request of their latest prices
BY_SYMBOL = True  # asked for by symbol, not by source_ref
BAR_LIMIT = 10000  # data points per page, the most Alpaca gives
ACTION_LIMIT = 1000
ACTION_TYPES = (
    "forward_split", "reverse_split", "unit_split", "cash_dividend", "stock_dividend", "spin_off", "cash_merger", "stock_merger",
    "stock_and_cash_merger", "name_change", "worthless_removal",
)
# The arrays of an answer and the kind each holds.
ACTION_ARRAYS = {
    "forward_splits": "forward_split", "reverse_splits": "reverse_split", "unit_splits": "unit_split",
    "cash_dividends": "cash_dividend", "stock_dividends": "stock_dividend", "spin_offs": "spin_off",
    "cash_mergers": "cash_merger", "stock_mergers": "stock_merger", "stock_and_cash_mergers": "stock_and_cash_merger",
    "name_changes": "name_change", "worthless_removals": "worthless_removal",
}
ACCEPT = "application/json"


def configured() -> bool:
    return all(source_credential(name) for name in KEY_VARIABLES)


def assets(http: PoliteHttp, exchange: str) -> list[Asset]:
    """Every US stock and ETF of the exchange, traded or not - one exchange per request, each answer a few MB."""
    data = _get(http, TRADING_HOST, "/v2/assets", {"asset_class": "us_equity", "exchange": exchange})
    if not isinstance(data, list):
        raise SourceRejected("Antwort von Alpaca ist keine Liste von Wertpapieren")
    found = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id") or not item.get("symbol"):
            continue
        name = " ".join(str(item.get("name") or item["symbol"]).split())[:200]
        listed = str(item.get("exchange") or exchange)
        found.append(Asset(
            str(item["id"]), str(item["symbol"]).upper(), listed, name, item.get("status") == "active", _text(item.get("cusip"), True),
            listed in ("ARCA", "NYSEARCA") or " ETF" in f" {name.upper()} ",
        ))
    return found


def calendar(http: PoliteHttp, start: date, end: date) -> list[Session]:
    """The trading days from `start` to `end`, with their opening and closing time."""
    data = _get(http, TRADING_HOST, "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
    if not isinstance(data, list):
        raise SourceRejected("Antwort von Alpaca ist kein Kalender")
    sessions = []
    for item in data:
        try:
            day = date.fromisoformat(item["date"])
            opens, closes = time.fromisoformat(item["open"]), time.fromisoformat(item["close"])
        except (KeyError, TypeError, ValueError):
            continue
        sessions.append(Session(
            day, datetime.combine(day, opens, NEW_YORK).astimezone(UTC), datetime.combine(day, closes, NEW_YORK).astimezone(UTC),
        ))
    return sessions


def daily_bars(
    http: PoliteHttp, symbols: Sequence[str], start: date, end: date, page_token: str | None = None, follow_renames: bool = True
) -> tuple[list[Bar], str | None]:
    """One page of the daily prices of the symbols from `start` to `end`, unadjusted, and the token of the next page
    (None: the last). `follow_renames`: a symbol's earlier prices under an earlier symbol too (Alpaca's `asof`); off for
    Wertpapiere no longer traded, whose symbol another may carry today."""
    query = {
        "symbols": ",".join(symbols), "timeframe": "1Day", "start": start.isoformat(), "end": end.isoformat(),
        "limit": BAR_LIMIT, "adjustment": "raw", "feed": "sip",
    } | ({} if follow_renames else {"asof": "-"}) | ({"page_token": page_token} if page_token else {})
    data = _get(http, DATA_HOST, "/v2/stocks/bars", query)
    if not isinstance(data, dict) or not isinstance(data.get("bars") or {}, dict):
        raise SourceRejected("Antwort von Alpaca enthält keine Tageskurse")
    bars = []
    for symbol, items in (data.get("bars") or {}).items():
        for item in items or []:
            bar = _bar(symbol, item)
            if bar is not None:
                bars.append(bar)
    return bars, data.get("next_page_token") or None


def corporate_actions(http: PoliteHttp, start: date, end: date, page_token: str | None = None) -> tuple[list[Action], str | None]:
    """One page of the corporate actions processed from `start` to `end`, and the token of the next page."""
    query = {
        "types": ",".join(ACTION_TYPES), "start": start.isoformat(), "end": end.isoformat(), "limit": ACTION_LIMIT,
    } | ({"page_token": page_token} if page_token else {})
    data = _get(http, DATA_HOST, "/v1/corporate-actions", query)
    if not isinstance(data, dict) or not isinstance(data.get("corporate_actions") or {}, dict):
        raise SourceRejected("Antwort von Alpaca enthält keine Kapitalmassnahmen")
    actions = []
    for array, kind in ACTION_ARRAYS.items():
        for item in (data.get("corporate_actions") or {}).get(array) or []:
            if isinstance(item, dict) and item.get("id"):
                actions.append(_action(kind, item))
    return actions, data.get("next_page_token") or None


def latest(http: PoliteHttp, symbols: Sequence[str]) -> tuple[list[Quote], list[Missing]]:
    """The latest price of each symbol from IEX - its last trade - with the day's open, high, low and volume so far and
    the last trading day's close; those it sent none for."""
    data = _get(http, DATA_HOST, "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": "iex"})
    if not isinstance(data, dict):
        raise SourceRejected("Antwort von Alpaca enthält keine Kurse")
    quotes, missing = [], []
    for symbol in symbols:
        snapshot = data.get(symbol) or {}
        trade = snapshot.get("latestTrade") or {}
        day, previous = snapshot.get("dailyBar") or {}, snapshot.get("prevDailyBar") or {}
        price, at = decimal_of(trade.get("p")), _time(trade.get("t"))
        if price is None or at is None:
            missing.append(Missing(symbol, "Kein Kurs von Alpaca (IEX): Symbol unbekannt oder ohne Handel"))
            continue
        volume = day.get("v")
        quotes.append(Quote(
            symbol, at, price, decimal_of(day.get("o")), decimal_of(day.get("h")), decimal_of(day.get("l")),
            int(volume) if isinstance(volume, int | float) and not isinstance(volume, bool) else None, decimal_of(previous.get("c")),
        ))
    return quotes, missing


def _get(http: PoliteHttp, host: str, path: str, query: dict) -> object:
    key_id, secret = (source_credential(name) for name in KEY_VARIABLES)
    if not key_id or not secret:
        raise SourceRejected(f"Kein Schlüssel: {' und '.join(KEY_VARIABLES)} fehlen in der Umgebung")
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
    response = http.get(f"https://{host}{path}?{urlencode(query)}", ACCEPT, permitted=True, headers=headers)
    try:
        return json.loads(response.content)
    except ValueError:
        raise SourceRejected("Antwort von Alpaca ist kein JSON") from None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # RFC 3339, possibly with nanoseconds: Python reads up to microseconds.
        text = value.replace("Z", "+00:00")
        if "." in text:
            whole, rest = text.split(".", 1)
            digits = "".join(char for char in rest if char.isdigit())
            zone = rest[len(digits):]
            text = f"{whole}.{digits[:6].ljust(6, '0')}{zone}"
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        return None


def _bar(symbol: str, item: dict) -> Bar | None:
    values = [decimal_of(item.get(key)) for key in ("o", "h", "l", "c")]
    at = _time(item.get("t"))
    if at is None or any(value is None for value in values):
        return None
    volume = item.get("v")
    return Bar(symbol.upper(), at, *values, int(volume) if isinstance(volume, int | float) and not isinstance(volume, bool) else None)


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _action(kind: str, item: dict) -> Action:
    if kind.endswith("merger"):
        roles = ("acquiree_", "acquirer_")
    elif kind == "spin_off":
        roles = ("source_", "new_")
    elif kind == "name_change":
        roles = ("new_",)
    else:
        roles = ("",)
    parties = [(_text(item.get(f"{role}symbol"), True), _text(item.get(f"{role}cusip"), True)) for role in roles]
    rate = item.get("rate", item.get("acquiree_rate"))
    return Action(
        str(item["id"]), kind, parties, _date(item.get("ex_date")),
        _date(item.get("effective_date")), _date(item.get("process_date")), decimal_of(item.get("old_rate")),
        decimal_of(item.get("new_rate")), decimal_of(rate), _text(item.get("old_symbol"), True), _text(item.get("new_symbol"), True),
        _text(item.get("currency"), True), item,
    )


def _text(value: object, upper: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper() if upper else value.strip()
