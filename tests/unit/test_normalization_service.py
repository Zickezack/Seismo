"""Normalization without a database: the main text of a page, PDF or plain text, the page's own metadata, the
language, the publication time."""
from datetime import UTC, datetime, timedelta

import pytest

from app.services import normalization_service
from app.services.normalization_service import clean_text, detect_language, page_metadata, parse_timestamp, read_page

GERMAN = (
    "Die Europäische Zentralbank hat die Leitzinsen am Donnerstag unverändert gelassen. Der Rat begründete den "
    "Entscheid mit der weiterhin hohen Unsicherheit über die Entwicklung der Inflation im Euroraum und kündigte an, "
    "die Daten der kommenden Monate genau zu verfolgen, bevor er über weitere Schritte entscheidet."
)
ENGLISH = (
    "The European Central Bank left its key interest rates unchanged on Thursday. The Governing Council said the "
    "outlook for inflation in the euro area remained uncertain and that it would follow the data of the coming months "
    "closely before deciding on further steps, a decision markets had widely expected."
)


def page(head="", body=f"<article><h1>Leitzins</h1><p>{GERMAN}</p><p>{GERMAN}</p></article>"):
    return f"""<html lang="de"><head><title>  Leitzins   bleibt | Bank </title>{head}</head>
<body><nav><a href="/">Start</a> <a href="/news">News</a> Menü Menü Menü</nav>{body}
<footer>Impressum · Datenschutz · Kontakt · Cookies</footer></body></html>""".encode()


def minimal_pdf(text):
    """A one-page PDF with the text in Helvetica: header, four objects, cross-reference table, trailer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_html_main_text_without_navigation_and_footer():
    result = read_page(page(), "text/html; charset=utf-8", "https://bank.example/a")

    assert "Leitzinsen am Donnerstag unverändert" in result.text
    assert "Impressum" not in result.text and "Menü" not in result.text
    assert result.title == "Leitzins bleibt | Bank"


def test_a_pdf_and_a_plain_text_are_read_too():
    assert read_page(minimal_pdf("Zinsentscheid der Nationalbank"), "application/pdf", "https://x.example/a.pdf").text == (
        "Zinsentscheid der Nationalbank"
    )
    latin = "Zürich, Genève\r\n\r\n\r\n   Bern  ".encode("latin-1")
    assert read_page(latin, "text/plain; charset=ISO-8859-1", "https://x.example/a.txt").text == "Zürich, Genève\n\nBern"


def test_a_pdf_that_is_none_raises():
    with pytest.raises(Exception):
        read_page(b"%PDF-1.4 broken", "application/pdf", "https://x.example/a.pdf")


def test_page_metadata_canonical_link_title_and_time():
    head = (
        '<link rel="Canonical" href="/news/a?utm_source=rss">'
        '<meta property="og:title" content="EZB: Leitzins bleibt">'
        '<meta property="article:published_time" content="2026-10-23T14:15:00+02:00">'
    )

    canonical, title, published = page_metadata(page(head), "https://bank.example/feed/a?x=1")

    assert canonical == "https://bank.example/news/a"
    assert title == "EZB: Leitzins bleibt"
    assert published == datetime(2026, 10, 23, 12, 15, tzinfo=UTC)


def test_a_page_without_a_main_text_says_nothing_of_the_article():
    head = (
        '<link rel="canonical" href="https://bank.example/home">'
        '<meta property="og:title" content="Just a moment...">'
        '<meta property="article:published_time" content="2026-10-23T14:15:00+02:00">'
    )

    result = read_page(page(head, body='<div id="app"></div>'), "text/html", "https://bank.example/news/a")

    assert result == normalization_service.Page("")


def test_time_from_json_ld_when_no_meta_tag_states_it():
    head = (
        '<script type="application/ld+json">{"@context": "https://schema.org", "@graph": '
        '[{"@type": "WebPage"}, {"@type": "NewsArticle", "datePublished": "2026-10-22T08:00:00Z"}]}</script>'
    )

    assert page_metadata(page(head), "https://bank.example/a")[2] == datetime(2026, 10, 22, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("head", "encoding", "header_charset"),
    [
        ('<meta charset="utf-8">', "utf-8", None),
        ('<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-9">', "iso-8859-9", None),
        ("", "utf-8", "UTF-8"),
        ("", "windows-1251", "windows-1251"),
        ("", "utf-8", None),
        ('<meta charset="no-such-charset">', "utf-8", None),
    ],
)
def test_the_title_is_read_in_the_page_s_charset(head, encoding, header_charset):
    title = "Fırından çıkan ürün" if encoding != "windows-1251" else "Украина вернула"
    content = f"<html><head>{head}<title>{title}</title></head><body><p>Text</p></body></html>".encode(encoding)

    assert page_metadata(content, "https://news.example/a", header_charset)[1] == title


def test_a_page_without_metadata():
    assert page_metadata(b"<p>nur Text</p>", "https://bank.example/a") == (None, None, None)
    assert page_metadata(b"", "https://bank.example/a") == (None, None, None)


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("2026-10-23T14:15:00Z", datetime(2026, 10, 23, 14, 15, tzinfo=UTC)),
        ("2026-10-23T14:15:00.123-04:00", datetime(2026, 10, 23, 18, 15, 0, 123000, tzinfo=UTC)),
        ("2026-10-23", None),  # a date alone says nothing about the time
        ("2026-10-23T14:15:00", None),  # no time zone
        ("1970-01-01T00:00:00Z", None),
        ((datetime.now(UTC) + timedelta(days=3)).isoformat(), None),
        ("gestern", None),
        (None, None),
    ],
)
def test_only_a_full_timestamp_with_time_zone_counts(raw, parsed):
    assert parse_timestamp(raw) == parsed


def test_language_when_there_is_enough_text_and_the_detection_is_sure():
    assert detect_language(GERMAN) == "de"
    assert detect_language(ENGLISH) == "en"
    assert detect_language("Leitzins bleibt") is None


def test_detection_knows_only_the_languages_gdelt_knows():
    # Not Guadeloupean Creole for a page that lists every EU language; 67 of GDELT's languages, Tibetan is missing.
    known = set(normalization_service._identifier().nb_classes)
    assert known <= set(normalization_service.GDELT_LANGUAGES.values()) and len(known) > 60 and "gcf" not in known


def test_clean_text():
    assert clean_text("  a   b \n\n\n\n c\t\td \r\n") == "a b\n\nc d"
