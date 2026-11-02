"""The shapes every price source returns."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Quote:
    """The latest price of a Wertpapier as the source sends it: unadjusted, its own time in UTC. `ref`: the
    Wertpapier's id at the source (instrument.source_ref), or its symbol where the source answers by symbol."""

    ref: str
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

    ref: str
    reason: str


@dataclass(frozen=True)
class Bar:
    """A trading day's prices as the source sends them, unadjusted: `at` the day's start as it gives it."""

    symbol: str
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None


@dataclass(frozen=True)
class Asset:
    """A Wertpapier as the source lists it."""

    ref: str  # its id at the source, kept through a change of symbol
    symbol: str
    exchange: str
    name: str
    active: bool
    cusip: str | None
    etf: bool


@dataclass(frozen=True)
class Action:
    """A corporate action as the source sends it; `parties` the Wertpapiere it concerns, each by its symbol then and
    its CUSIP (None where not sent) - a merger's both, the acquired one first; a change of symbol's under the new one."""

    ref: str
    kind: str
    parties: list[tuple[str | None, str | None]]
    ex_date: date | None
    effective_date: date | None
    process_date: date | None
    old_rate: Decimal | None
    new_rate: Decimal | None
    rate: Decimal | None
    old_symbol: str | None
    new_symbol: str | None
    currency: str | None
    details: dict


@dataclass(frozen=True)
class Session:
    """A trading day with its opening and closing time, in UTC."""

    day: date
    opens_at: datetime
    closes_at: datetime


def decimal_of(value: object) -> Decimal | None:
    """A number as a source sends it - JSON number or text -, None for anything else ("NA", null, NaN)."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() else None
