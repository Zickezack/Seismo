"""The canonical form of an article link (app/services/urls.py)."""
import pytest

from app.services.urls import clean_url


@pytest.mark.parametrize(
    ("url", "clean"),
    [
        ("https://www.aljazeera.com/news/2026/10/23/a?traffic_source=rss", "https://www.aljazeera.com/news/2026/10/23/a"),
        ("https://x.example/a?id=7&utm_source=rss&utm_medium=feed&page=2", "https://x.example/a?id=7&page=2"),
        ("https://x.example/a?fbclid=abc&GCLID=x", "https://x.example/a"),
        ("HTTPS://WWW.Example.COM:443/Path/A#section", "https://www.example.com/Path/A"),
        ("http://example.com:80", "http://example.com/"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://x.example/search?q=a+b&empty=", "https://x.example/search?q=a+b&empty="),
        ("https://[2001:db8::1]:443/a", "https://[2001:db8::1]/a"),
        ("  https://x.example/a  ", "https://x.example/a"),
    ],
)
def test_clean_url(url, clean):
    assert clean_url(url) == clean


@pytest.mark.parametrize("url", ["ftp://x.example/a", "mailto:a@b.example", "not a url", "https://x.example:port/a"])
def test_anything_but_an_http_url_comes_back_as_it_is(url):
    assert clean_url(url) == url


def test_the_page_named_by_its_parameters_stays_the_same():
    # Parameter order and the ones that name the page are kept.
    assert clean_url("https://x.example/article.php?b=2&a=1") == "https://x.example/article.php?b=2&a=1"
