"""The Wertpapier form and how a price is shown, without a database (app/services/instrument_service.py)."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import instrument_service
from app.services.instrument_service import InvalidInstrument, PriceView, number_label

FORM = {"symbol": " nesn ", "exchange": "sw", "name": "Nestlé  S.A.", "kind": "stock", "currency": "chf", "isin": "CH0038863350"}


def test_a_wertpapier_is_taken_as_the_source_names_it():
    values = instrument_service.parse(FORM)

    assert (values.symbol, values.exchange, values.name, values.currency, values.isin) == ("NESN", "SW", "Nestlé S.A.", "CHF", "CH0038863350")


@pytest.mark.parametrize(("changes", "message"), [
    ({"symbol": ""}, "Symbol:"),
    ({"exchange": "S W"}, "Börse:"),
    ({"name": " "}, "Name:"),
    ({"kind": "bond"}, "Art:"),
    ({"currency": ""}, "Währung: drei Buchstaben"),
    ({"kind": "index", "currency": "USD"}, "Währung: ein Index zählt in Punkten"),
    ({"isin": "CH0038863351"}, "ISIN:"),
])
def test_an_entry_that_is_not_one_names_its_field(changes, message):
    with pytest.raises(InvalidInstrument, match=f"^{message}"):
        instrument_service.parse(FORM | changes)


def test_an_index_counts_in_points_without_a_currency():
    assert instrument_service.parse(FORM | {"kind": "index", "currency": ""}).currency is None


def test_a_price_names_its_time_exchange_source_delay_and_currency():
    at = datetime(2026, 10, 30, 20, 29, tzinfo=UTC)
    share = PriceView(Decimal("1234.5"), Decimal("1200"), at, at, "eodhd", True, "CHF", "SW")
    index = PriceView(Decimal("5998.04"), None, at, at, "eodhd", True, None, "INDX")

    assert (share.price_label, share.change_label, share.exchange_label) == ("1 234,50 CHF", "+2,88 %", "SIX Swiss Exchange")
    assert share.source_label == "EODHD, verzögert, 15 bis 20 Minuten"
    assert (index.price_label, index.change_label, index.exchange_label) == ("5 998,04 Punkte", "", "Index")
    assert [number_label(value) for value in (None, Decimal("0.123456"), Decimal("-3.5"))] == ["–", "0,1235", "-3,50"]
