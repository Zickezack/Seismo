"""The canonical form of an article link: tracking parameters, fragment and default port gone, scheme and host in
lower case (userstories.md, Normalisierung, story 1). The full text is fetched from it; the link as the source
gave it stays on the article.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only say who sent a reader, never which page: dropped. Kept short and well known, since a
# parameter that names the page must stay.
_TRACKING = {
    "at_campaign", "at_medium", "cmpid", "dclid", "fbclid", "gbraid", "gclid", "igshid", "mc_cid", "mc_eid", "msclkid",
    "ocid", "ref_src", "smid", "traffic_source", "twclid", "wbraid", "yclid", "_ga", "_gl",
}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def clean_url(url: str) -> str:
    """The canonical form of an http(s) URL; anything else comes back as it is."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return url
    host = parts.hostname
    if ":" in host:  # IPv6
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:  # a port that is no number
        return url
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if not _tracking(key)]
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(query, doseq=True), ""))


def _tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith("utm_") or key in _TRACKING
