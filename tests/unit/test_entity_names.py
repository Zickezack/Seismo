"""Name keys and identifier checks of the entities (app/services/entity_names.py)."""
import pytest

from app.services.entity_names import country_code, identifier_key, name_key, stated_in


@pytest.mark.parametrize(("one", "other"), [
    ("The Boeing Company", "Boeing Co."),
    ("U.S. Steel", "US Steel"),
    ("Siemens AG", "SIEMENS"),
    ("Toyota Motor Corp.", "toyota motor"),
    ("Luiz Inácio Lula da Silva", "Luiz Inacio Lula da Silva"),
    ("Kim Jong-un", "Kim Jong Un"),
    ("Lowe's Companies, Inc.", "Lowes Companies"),
    ("S&P Global", "S and P Global"),
])
def test_spellings_of_one_name_share_their_key(one, other):
    assert name_key(one) == name_key(other)


@pytest.mark.parametrize(("one", "other"), [
    ("Volodymyr Zelenskyy", "Vladimir Zelensky"),  # a transliteration: the LLM judges it, no key makes it equal
    ("Bank of Japan", "Bank of Korea"),
    ("Gazprom", "Gazprom Neft"),
])
def test_different_names_keep_different_keys(one, other):
    assert name_key(one) != name_key(other)


def test_a_legal_form_alone_or_the_alone_stays_a_name_and_punctuation_alone_is_none():
    assert (name_key("The"), name_key("AG"), name_key("–"), name_key("中国人民银行")) == ("the", "ag", "", "中国人民银行")


@pytest.mark.parametrize(("scheme", "value", "key"), [
    ("isin", "US0378331005", "US0378331005"),
    ("isin", "ch 0012032048", "CH0012032048"),
    ("isin", "US0378331006", None),  # wrong check digit
    ("isin", "US037833100", None),
    ("lei", "HWUPKR0MPOU8FGXBT394", "HWUPKR0MPOU8FGXBT394"),
    ("lei", "HWUPKR0MPOU8FGXBT395", None),
    ("ticker", "nasdaq: aapl", "NASDAQ:AAPL"),
    ("ticker", "SIX:NESN", "SIX:NESN"),
    ("ticker", "AAPL", None),  # without its exchange it names no one security
    ("name", "Apple", None),
])
def test_an_identifier_counts_only_when_valid_in_its_one_spelling(scheme, value, key):
    assert identifier_key(scheme, value) == key


def test_an_identifier_counts_only_when_the_quote_states_it():
    assert stated_in("ticker", "NYSE:BA", "Shares of Boeing (NYSE: BA) fell 3%.")
    assert not stated_in("ticker", "NYSE:BA", "NYSE-listed Boeing fell 3%, said a BAE spokesman.")
    assert stated_in("isin", "US0378331005", "ISIN US 0378331005")
    assert not stated_in("isin", "US0378331005", "Apple Inc. reported results.")


def test_a_country_code_must_be_assigned_and_the_reserved_ones_are_replaced():
    assert [country_code(code) for code in ("ua", "UK", "EL", "XK", "EU", "ZZ", None)] == ["UA", "GB", "GR", "XK", None, None, None]
