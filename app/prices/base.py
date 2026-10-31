"""The shape every price source returns."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Quote:
    """The latest price of a Wertpapier as the source sends it: unadjusted, its own time in UTC."""

    symbol: str
    exchange: str
    at: datetime
    close: Decimal  # the price
    open: Decimal | None = None  # the day's so far
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int | None = None
    previous_close: Decimal | None = None  # the last trading day's close


@dataclass(frozen=True)
class Missing:
    """A Wertpapier the source sent no price for, and why, in the adapter's words."""

    symbol: str
    exchange: str
    reason: str
