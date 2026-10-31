"""EODHD's live prices (https://eodhd.com/financial-apis/live-realtime-stocks-api): stocks, ETFs and indices of
exchanges worldwide, delayed 15 to 20 minutes, several Wertpapiere per request - "AAPL.US" is the symbol AAPL at the
exchange code US, an index's exchange code is INDX.

The key comes from the environment (EODHD_API_KEY), never from a Quelle or the log. EODHD's robots.txt disallows
/api/ for every crawler; a request with the key registered for this application is EODHD's permission, so it goes
with `permitted` and only to EODHD's API host - the request slots and pauses still apply (architecture.md, the
admin's decision of 2026-10-31).
"""
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from app.config import source_credential
from app.prices.base import Missing, Quote
from app.services.fetch_policy_service import PoliteHttp
from app.sources.errors import SourceRejected

NAME = "eodhd"
LABEL = "EODHD"
KEY_VARIABLE = "EODHD_API_KEY"
API_HOST = "eodhd.com"
DELAYED = True
DELAY_LABEL = "verzögert, 15 bis 20 Minuten"
# Wertpapiere per request: EODHD counts each as one call of the day's quota either way.
BATCH = 20
ACCEPT = "application/json"


def configured() -> bool:
    return bool(source_credential(KEY_VARIABLE))


def latest(http: PoliteHttp, instruments: Sequence[tuple[str, str]]) -> tuple[list[Quote], list[Missing]]:
    """The latest prices of up to BATCH Wertpapiere, (symbol, exchange) each, in one request; those it sent none for.
    SourceRejected without the key or for an answer that is no list of prices; what PoliteHttp.get raises."""
    key = source_credential(KEY_VARIABLE)
    if not key:
        raise SourceRejected(f"Kein Schlüssel: {KEY_VARIABLE} fehlt in der Umgebung")
    codes = [f"{symbol}.{exchange}" for symbol, exchange in instruments]
    query = {"api_token": key, "fmt": "json"} | ({"s": ",".join(codes[1:])} if len(codes) > 1 else {})
    response = http.get(f"https://{API_HOST}/api/real-time/{codes[0]}?{urlencode(query)}", ACCEPT, permitted=True)
    try:
        data = json.loads(response.content)
    except ValueError:
        raise SourceRejected("Antwort von EODHD ist kein JSON") from None
    items = data if isinstance(data, list) else [data]
    if not all(isinstance(item, dict) for item in items):
        raise SourceRejected("Antwort von EODHD ist keine Liste von Kursen")
    by_code = {str(item.get("code", "")).upper(): item for item in items}
    quotes, missing = [], []
    for (symbol, exchange), code in zip(instruments, codes, strict=True):
        item = by_code.get(code.upper())
        quote = _quote(symbol, exchange, item) if item else None
        if quote is None:
            missing.append(Missing(symbol, exchange, "Kein Kurs von EODHD: Symbol oder Börse unbekannt, oder im Abo nicht enthalten"))
        else:
            quotes.append(quote)
    return quotes, missing


def _quote(symbol: str, exchange: str, item: dict) -> Quote | None:
    """A price as EODHD sends it; None when it has none ("NA")."""
    close, timestamp = _decimal(item.get("close")), item.get("timestamp")
    if close is None or not isinstance(timestamp, int | float) or isinstance(timestamp, bool):
        return None
    volume = item.get("volume")
    return Quote(
        symbol, exchange, datetime.fromtimestamp(timestamp, UTC), close, _decimal(item.get("open")), _decimal(item.get("high")),
        _decimal(item.get("low")), int(volume) if isinstance(volume, int | float) and not isinstance(volume, bool) else None,
        _decimal(item.get("previousClose")),
    )


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None
