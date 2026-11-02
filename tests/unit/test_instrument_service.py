"""The Wertpapier form and how a price and a corporate action are shown, without a database
(app/services/instrument_service.py)."""
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services import instrument_service
from app.services.instrument_service import ActionView, InvalidInstrument, PriceView, number_label

SIX = {"source": "six", "symbol": " nesn ", "valor_id": "ch0038863350chf4", "name": "Nestlé  S.A.", "kind": "stock", "currency": "chf"}
EODHD = {"source": "eodhd", "symbol": "sap", "exchange": "xetra", "name": "SAP", "kind": "stock", "currency": "eur"}


def test_a_swiss_wertpapier_is_taken_by_its_valor_id_with_the_isin_from_it():
    values = instrument_service.parse(SIX)

    assert (values.source, values.source_ref, values.symbol, values.exchange, values.name, values.currency, values.isin) == (
        "six", "CH0038863350CHF4", "NESN", "SIX", "Nestlé S.A.", "CHF", "CH0038863350",
    )


def test_one_of_eodhd_is_taken_by_its_code():
    values = instrument_service.parse(EODHD)

    assert (values.source, values.source_ref, values.exchange) == ("eodhd", "SAP.XETRA", "XETRA")


@pytest.mark.parametrize(("form", "message"), [
    (SIX | {"source": "alpaca"}, "Quelle:"),
    (SIX | {"symbol": ""}, "Symbol:"),
    (SIX | {"valor_id": "CH0038863350"}, "ValorId:"),
    (SIX | {"valor_id": "CH0038863351CHF4"}, "ValorId:"),
    (EODHD | {"exchange": "X E"}, "Börse:"),
    (SIX | {"name": " "}, "Name:"),
    (SIX | {"kind": "bond"}, "Art:"),
    (SIX | {"currency": ""}, "Währung: drei Buchstaben"),
    (SIX | {"kind": "index", "currency": "CHF"}, "Währung: ein Index zählt in Punkten"),
    (SIX | {"isin": "CH0038863351"}, "ISIN:"),
])
def test_an_entry_that_is_not_one_names_its_field(form, message):
    with pytest.raises(InvalidInstrument, match=f"^{message}"):
        instrument_service.parse(form)


def test_a_changed_wertpapier_keeps_its_source():
    assert instrument_service.parse(EODHD | {"source": "six"}, "eodhd").source == "eodhd"


def test_an_index_counts_in_points_without_a_currency():
    assert instrument_service.parse(SIX | {"kind": "index", "currency": ""}).currency is None


def test_a_day_asked_for_is_what_was_known_after_its_close_in_new_york():
    assert instrument_service.parse_as_of("2024-06-07") == datetime(2024, 6, 8, 3, 59, 59, tzinfo=UTC)
    assert instrument_service.parse_as_of("") is None and instrument_service.parse_as_of("7.6.2024") is None


def test_a_price_names_its_time_exchange_source_delay_and_currency():
    at = datetime(2026, 10, 30, 20, 29, tzinfo=UTC)
    share = PriceView(Decimal("1234.5"), Decimal("1200"), at, at, "six", True, "CHF", "SIX")
    index = PriceView(Decimal("5998.04"), None, at, at, "eodhd", True, None, "INDX")
    us = PriceView(Decimal("270.1"), None, at, at, "alpaca", False, "USD", "NASDAQ")

    assert (share.price_label, share.change_label, share.exchange_label) == ("1 234,50 CHF", "+2,88 %", "SIX Swiss Exchange")
    assert share.source_label == "SIX, verzögert, 15 Minuten"
    assert (index.price_label, index.change_label, index.exchange_label) == ("5 998,04 Punkte", "", "Index")
    assert index.source_label == "EODHD, verzögert, 15 bis 20 Minuten"
    assert (us.source_label, us.exchange_label) == ("Alpaca, Echtzeit", "Nasdaq")
    assert [number_label(value) for value in (None, Decimal("0.123456"), Decimal("-3.5"))] == ["–", "0,1235", "-3,50"]


def test_a_corporate_action_says_what_it_did():
    def action(kind, **values):
        fields = dict.fromkeys(("ex_date", "effective_date", "process_date", "old_rate", "new_rate", "rate", "old_symbol",
                                "new_symbol", "currency"))
        return ActionView(kind, **(fields | values))

    split = action("forward_split", ex_date=date(2024, 6, 10), old_rate=Decimal(1), new_rate=Decimal(10))
    assert (split.label, split.summary, split.day) == ("Aktiensplit", "1 : 10", date(2024, 6, 10))
    assert action("reverse_split", old_rate=Decimal("20"), new_rate=Decimal(1)).summary == "20 : 1"
    assert action("name_change", old_symbol="FB", new_symbol="META", process_date=date(2022, 6, 9)).summary == "FB → META"
    assert action("cash_dividend", rate=Decimal("0.25"), currency="USD").summary == "0,2500 USD"
    assert action("cash_merger", rate=Decimal("54.2"), effective_date=date(2022, 10, 28)).summary == "Verhältnis 54,2"
