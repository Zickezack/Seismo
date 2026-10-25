"""Checks of the source form (userstories.md, Quellenadapter und Ingestion, story 1)."""
import pytest

from app.services import source_service
from app.services.source_service import NEW_SOURCE_FORM, InvalidSource, SourceValues

FORM = {
    "name": "  EZB – Pressemitteilungen ",
    "adapter_type": "rss",
    "url": " https://www.ecb.europa.eu/rss/press.html ",
    "is_active": "on",
    "extract_events": "on",
    "fetch_interval_minutes": "60",
    "language": "en",
    "terms_note": "  Wiedergabe mit Quellenangabe erlaubt. ",
    "raw_text_retention_days": "30",
    "region": "europe",
}


def test_valid_form_is_trimmed():
    assert source_service.parse(FORM) == SourceValues(
        name="EZB – Pressemitteilungen",
        adapter_type="rss",
        url="https://www.ecb.europa.eu/rss/press.html",
        query=None,
        is_active=True,
        extract_events=True,
        fetch_interval_minutes=60,
        language="en",
        terms_note="Wiedergabe mit Quellenangabe erlaubt.",
        raw_text_retention_days=30,
        min_request_interval_seconds=None,
        region="europe",
        quality=None,
    )


def test_unticked_checkbox_empty_language_and_empty_note():
    values = source_service.parse({key: value for key, value in FORM.items() if key != "is_active"} | {"language": "", "terms_note": "  "})

    assert (values.is_active, values.language, values.terms_note) == (False, None, None)


@pytest.mark.parametrize(
    ("field", "raw", "message"),
    [
        ("name", "   ", "Name: bitte angeben."),
        ("name", "x" * 201, "Name: höchstens 200 Zeichen."),
        ("adapter_type", "twitter", "Typ: bitte einen der angebotenen Werte wählen."),
        ("adapter_type", "", "Typ: bitte einen der angebotenen Werte wählen."),
        ("language", "xx", "Sprache: bitte einen der angebotenen Werte wählen."),
        ("terms_note", "x" * 2001, "Nutzungsbedingungen: höchstens 2000 Zeichen."),
        ("url", "https://example.org/" + "x" * 2000, "URL: höchstens 2000 Zeichen."),
    ],
)
def test_invalid_field_is_named(field, raw, message):
    with pytest.raises(InvalidSource) as error:
        source_service.parse(FORM | {field: raw})

    assert str(error.value) == message


@pytest.mark.parametrize(
    "url",
    ["", "www.ecb.europa.eu/rss", "ftp://example.org/feed", "javascript:alert(1)", "https://", "https://exa mple.org/", "http://[::1"],
)
def test_url_needs_http_or_https_and_a_host(url):
    with pytest.raises(InvalidSource) as error:
        source_service.parse(FORM | {"url": url})

    assert str(error.value) == "URL: bitte eine vollständige Adresse mit http:// oder https:// angeben."


@pytest.mark.parametrize(("raw", "valid"), [("5", True), ("1440", True), ("4", False), ("1441", False), ("1.5", False), ("", False)])
def test_fetch_interval_range(raw, valid):
    if valid:
        assert source_service.parse(FORM | {"fetch_interval_minutes": raw}).fetch_interval_minutes == int(raw)
    else:
        with pytest.raises(InvalidSource, match=r"^Abrufintervall \(Minuten\): bitte eine ganze Zahl von 5 bis 1440 eingeben\.$"):
            source_service.parse(FORM | {"fetch_interval_minutes": raw})


@pytest.mark.parametrize(("raw", "valid"), [("0", True), ("365", True), ("-1", False), ("366", False), ("dreissig", False)])
def test_raw_text_retention_range(raw, valid):
    if valid:
        assert source_service.parse(FORM | {"raw_text_retention_days": raw}).raw_text_retention_days == int(raw)
    else:
        with pytest.raises(InvalidSource, match=r"^Rohtext aufbewahren \(Tage\): bitte eine ganze Zahl von 0 bis 365 eingeben\.$"):
            source_service.parse(FORM | {"raw_text_retention_days": raw})


@pytest.mark.parametrize(
    ("raw", "expected"), [("", None), ("  ", None), ("1", 1), (" 600 ", 600), ("0", "error"), ("601", "error"), ("2.5", "error")]
)
def test_min_request_interval_is_optional_within_its_range(raw, expected):
    if expected == "error":
        with pytest.raises(
            InvalidSource, match=r"^Mindestabstand \(Sekunden\): bitte eine ganze Zahl von 1 bis 600 eingeben oder leer lassen\.$"
        ):
            source_service.parse(FORM | {"min_request_interval_seconds": raw})
    else:
        assert source_service.parse(FORM | {"min_request_interval_seconds": raw}).min_request_interval_seconds == expected


GDELT = FORM | {"adapter_type": "gdelt", "url": "", "query": ' "central bank" sourcelang:german '}


def test_gdelt_source_gets_the_api_address_and_keeps_its_query():
    values = source_service.parse(GDELT)

    assert (values.adapter_type, values.url, values.query) == (
        "gdelt", "https://api.gdeltproject.org/api/v2/doc/doc", '"central bank" sourcelang:german'
    )
    # An address of the admin's own stays.
    assert source_service.parse(GDELT | {"url": "https://gdelt.example/doc"}).url == "https://gdelt.example/doc"


@pytest.mark.parametrize(
    ("form", "message"),
    [
        (GDELT | {"query": "  "}, "Suchanfrage: bitte angeben."),
        (GDELT | {"query": "x" * 1001}, "Suchanfrage: höchstens 1000 Zeichen."),
        (FORM | {"query": "inflation"}, "Suchanfrage: nur für die Typen GDELT, ReliefWeb."),
    ],
)
def test_query_is_required_for_gdelt_and_not_allowed_for_rss(form, message):
    with pytest.raises(InvalidSource) as error:
        source_service.parse(form)

    assert str(error.value) == message


RELIEFWEB = FORM | {"adapter_type": "reliefweb", "url": ""}


@pytest.mark.parametrize(("query", "expected"), [("  ", None), (" country:Sudan ", "country:Sudan")])
def test_reliefweb_source_gets_the_api_address_and_an_optional_query(query, expected):
    values = source_service.parse(RELIEFWEB | {"query": query})

    assert (values.url, values.query) == ("https://api.reliefweb.int/v2/reports", expected)


@pytest.mark.parametrize("parameter", ["api_key", "API_KEY", "apikey", "access_token"])
def test_a_url_with_an_api_key_is_refused_it_belongs_into_the_environment(parameter):
    url = f"https://api.eia.gov/v2/natural-gas/stor/wkly/data/?frequency=weekly&{parameter}=SECRET"

    with pytest.raises(InvalidSource, match=r"^URL: ohne API-Key angeben, er gehört in die Umgebung \(README\)\.$"):
        source_service.parse(FORM | {"adapter_type": "eia", "url": url})


def test_eia_source_takes_the_api_url_of_a_data_set_without_query():
    url = "https://api.eia.gov/v2/natural-gas/stor/wkly/data/?frequency=weekly&data[0]=value"

    values = source_service.parse(FORM | {"adapter_type": "eia", "url": url})

    assert (values.adapter_type, values.url, values.query) == ("eia", url, None)


def test_rss_needs_its_url():
    with pytest.raises(InvalidSource, match="^URL: bitte eine vollständige Adresse"):
        source_service.parse(FORM | {"url": ""})


def test_every_error_of_the_form_at_once():
    with pytest.raises(InvalidSource) as error:
        source_service.parse({})

    assert str(error.value) == (
        "Name: bitte angeben. Typ: bitte einen der angebotenen Werte wählen. "
        "URL: bitte eine vollständige Adresse mit http:// oder https:// angeben. "
        "Abrufintervall (Minuten): bitte eine ganze Zahl von 5 bis 1440 eingeben. "
        "Region: bitte einen der angebotenen Werte wählen. "
        "Rohtext aufbewahren (Tage): bitte eine ganze Zahl von 0 bis 365 eingeben."
    )


def test_form_for_a_new_source_starts_with_valid_defaults():
    form = {key: str(value) if not isinstance(value, bool) else "on" for key, value in NEW_SOURCE_FORM.items()}

    values = source_service.parse(form | {"name": "Neu", "url": "https://example.org/feed", "region": "worldwide"})

    assert (values.is_active, values.fetch_interval_minutes, values.raw_text_retention_days) == (True, 60, 30)
    assert values.min_request_interval_seconds is None
