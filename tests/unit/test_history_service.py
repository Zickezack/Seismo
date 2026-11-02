"""The rules of the historical market data without a database (app/services/history_service.py): the period, the ISIN
of a CUSIP, the company of a listed name, the batches, and the split adjustment of a point in time."""
from collections import namedtuple
from datetime import date
from decimal import Decimal

from app.services import history_service
from app.services.history_service import Split, split_adjusted

Due = namedtuple("Due", "id symbol status delisted_on history_through")
Bar = namedtuple("Bar", "day open high low close volume")


def test_the_period_is_the_running_year_and_the_ones_before():
    assert history_service.period_start(date(2026, 10, 31), 11) == date(2016, 1, 1)
    assert history_service.period_start(date(2026, 10, 31), 1) == date(2026, 1, 1)


def test_a_us_cusip_gives_its_isin_with_the_check_digit():
    assert history_service.isin_of("037833100") == "US0378331005"  # Apple
    assert history_service.isin_of("67066G104") == "US67066G1040"  # NVIDIA
    assert history_service.isin_of(None) is None and history_service.isin_of("1234") is None


def test_a_listed_name_gives_its_company_and_no_company_for_other_securities():
    assert history_service.company_name("Apple Inc. Common Stock") == "Apple Inc."
    assert history_service.company_name("Alphabet Inc. Class A Common Stock") == "Alphabet Inc."
    assert history_service.company_name("Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares") == (
        "Taiwan Semiconductor Manufacturing Company Ltd."
    )
    assert history_service.company_name("Twitter, Inc.") == "Twitter, Inc."
    assert history_service.company_name("Acme Corp. Warrants") is None
    assert history_service.company_name("Bank of America Corp 6.0% Depositary Shares Preferred") is None


def test_a_batch_shares_its_start_and_whether_traded_and_asks_for_each_symbol_once():
    due = [
        Due(1, "AAPL", "active", None, None), Due(2, "MSFT", "active", None, None), Due(3, "AAPL", "active", None, None),
        Due(4, "TWTR", "inactive", None, None), Due(5, "NVDA", "active", None, date(2026, 10, 29)),
    ]

    assert [row.id for row in history_service.next_batch(due)] == [1, 2]
    assert [row.id for row in history_service.next_batch(due[3:])] == [4]


def test_a_batch_holds_at_most_the_symbols_of_one_request():
    due = [Due(number, f"S{number}", "active", None, None) for number in range(250)]

    assert len(history_service.next_batch(due)) == 200


def test_a_split_adjusts_the_prices_before_its_day_only():
    bars = [
        Bar(date(2024, 6, 10), Decimal(121), Decimal(122), Decimal(118), Decimal("121.8"), 300),
        Bar(date(2024, 6, 7), Decimal(1200), Decimal(1220), Decimal(1180), Decimal("1208.9"), 40),
    ]

    known = split_adjusted(bars, [Split(date(2024, 6, 10), Decimal(1) / Decimal(10))])

    assert [(bar.day, bar.factor, bar.adjusted_close) for bar in known] == [
        (date(2024, 6, 10), Decimal(1), Decimal("121.8")), (date(2024, 6, 7), Decimal("0.1"), Decimal("120.89")),
    ]
    # As received: the price itself stays.
    assert known[1].close == Decimal("1208.9")
