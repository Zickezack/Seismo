"""Whether a publisher allows text and data mining of a full text (userstories.md, Quellenadapter und Ingestion,
story 9): the TDM Reservation Protocol (W3C Community Group report, TDMRep) and a list of domains whose terms forbid
it. A reserved full text is not kept; the article keeps its metadata.

TDMRep in its order of precedence: the host's /.well-known/tdmrep.json, checked before the page is requested (its
first matching rule counts); then the page's HTTP headers; then its HTML meta tags. A later one overrides an earlier
one, a missing one leaves it as it is. tdm-reservation 1 reserves the rights, 0 does not, anything else is unset.
EPUB and PDF metadata are not read.
"""
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from app.services import robots_txt

TDMREP_PATH = "/.well-known/tdmrep.json"
TDMREP_MAX_BYTES = 100 * 1024
# The meta tags stand in the head; a page is searched no further.
META_SEARCH_BYTES = 256 * 1024
# Domains found in October 2026 whose terms forbid text and data mining, scraping or AI use of their articles: the
# list a new instance starts with (Plattform-Einstellung "Domains ohne Volltext").
STARTER_BLOCKED_DOMAINS = (
    "bbc.co.uk", "bbc.com", "cnbc.com", "defensenews.com", "france24.com", "freightwaves.com", "indiatimes.com",
    "oilprice.com", "rigzone.com", "scmp.com", "srf.ch", "tagesschau.de", "theguardian.com", "theloadstar.com",
)
MAX_DOMAINS = 500
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


@dataclass(frozen=True)
class Layer:
    """What one place says: the reservation (None: unset) and a policy link, if any."""

    via: str
    reserved: bool | None = None
    policy: str | None = None


@dataclass(frozen=True)
class Verdict:
    reserved: bool
    via: str | None = None
    policy: str | None = None

    @property
    def reason(self) -> str:
        policy = f"; Bedingungen: {self.policy}" if self.policy else ""
        return f"Text- und Data-Mining vorbehalten ({self.via}){policy}"


def decide(*layers: Layer) -> Verdict:
    """Fold the places in their order of precedence: a later one overrides, a missing value leaves it as it is."""
    reserved, via, policy = None, None, None
    for layer in layers:
        if layer.reserved is not None:
            reserved, via = layer.reserved, layer.via
        if layer.policy:
            policy = layer.policy
    return Verdict(bool(reserved), via, policy if reserved else None)


def parse_tdmrep(content: bytes) -> str | None:
    """The rules of a tdmrep.json, normalised as JSON for the cache; None for a file that is not valid TDMRep."""
    try:
        data = json.loads(content.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    rules = []
    for rule in data:
        if not isinstance(rule, dict) or not isinstance(rule.get("location"), str):
            continue
        rules.append(
            {
                "location": robots_txt.normalise(rule["location"]),
                "reserved": _reservation(rule.get("tdm-reservation")),
                "policy": rule.get("tdm-policy") if isinstance(rule.get("tdm-policy"), str) else None,
            }
        )
    return json.dumps(rules)


def file_layer(rules_json: str | None, url: str) -> Layer:
    """The first rule of the host's tdmrep.json whose location matches the URL; unset without one."""
    path = robots_txt.path_of(url)
    for rule in json.loads(rules_json) if rules_json else []:
        if robots_txt.pattern_matches(rule["location"], path):
            return Layer("tdmrep.json", rule["reserved"], rule["policy"])
    return Layer("tdmrep.json")


def header_layer(headers: Mapping[str, str]) -> Layer:
    """The tdm-reservation and tdm-policy headers of a response (lower-case names)."""
    return Layer("HTTP-Header", _reservation(headers.get("tdm-reservation")), headers.get("tdm-policy") or None)


def meta_layer(content: bytes, content_type: str | None) -> Layer:
    """The tdm-reservation and tdm-policy meta tags of an HTML page; unset for anything else."""
    media_type = (content_type or "").split(";")[0].strip().lower()
    if media_type not in ("text/html", "application/xhtml+xml"):
        return Layer("HTML-Meta")
    finder = _MetaFinder()
    finder.feed(content[:META_SEARCH_BYTES].decode("utf-8", errors="replace"))
    return Layer("HTML-Meta", _reservation(finder.values.get("tdm-reservation")), finder.values.get("tdm-policy") or None)


def blocked_domain(url: str, domains: Iterable[str]) -> str | None:
    """The listed domain the URL's host is or lies under, e.g. "bbc.co.uk" for www.bbc.co.uk."""
    host = (urlsplit(url).hostname or "").lower()
    return next((domain for domain in domains if host == domain or host.endswith(f".{domain}")), None)


def parse_domains(raw: str) -> list[str]:
    """One domain per line, sorted and each once; ValueError names the lines that are no domain."""
    domains, invalid = set(), []
    for line in raw.splitlines():
        domain = line.strip().lower().removeprefix("*.").removeprefix(".")
        if not domain:
            continue
        if _DOMAIN.fullmatch(domain):
            domains.add(domain)
        else:
            invalid.append(line.strip())
    if invalid:
        raise ValueError(f"keine Domain: {', '.join(invalid[:5])}{' …' if len(invalid) > 5 else ''}")
    if len(domains) > MAX_DOMAINS:
        raise ValueError(f"höchstens {MAX_DOMAINS} Domains")
    return sorted(domains)


def _reservation(raw: object) -> bool | None:
    """1 reserves, 0 does not; anything else is a protocol error and counts as unset."""
    if isinstance(raw, bool):  # JSON true is no 1
        return None
    value = raw.strip() if isinstance(raw, str) else raw
    if value in (1, "1"):
        return True
    if value in (0, "0"):
        return False
    return None


class _MetaFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = dict(attrs)
        name = (attributes.get("name") or "").strip().lower()
        if name in ("tdm-reservation", "tdm-policy") and name not in self.values:
            self.values[name] = (attributes.get("content") or "").strip()
