"""robots.txt by RFC 9309 with the precedence of Google's reference parser: of all rules that match a URL, the
longest rule wins, an Allow on a tie. Python's urllib.robotparser (3.14) weighs a wildcard rule by how much of the
URL it matched instead, so "Disallow: /api/*" beat "Allow: /api/v1/rss-outbound-feed" and an allowed feed looked
disallowed.

Also: the group named for our product token, else the "*" group, several groups of one name merged; `*` matches any
characters, a trailing `$` the end of the URL; percent-encoding compared normalised; Crawl-delay of the chosen group.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote, unquote, urlsplit

# Characters a path keeps as they are; the rest is compared percent-encoded. `*` and `$` stay for the patterns.
_SAFE = "/?=&;:@+,!'()*$~"


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str  # normalised, with `*` and an optional trailing `$`

    @property
    def priority(self) -> int:
        return len(self.pattern)

    def matches(self, path: str) -> bool:
        return pattern_matches(self.pattern, path)


@dataclass
class Group:
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def allows(robots_txt: str, product_token: str, url: str) -> bool:
    """Whether the robots.txt lets the product token fetch the URL."""
    path = path_of(url)
    if path == "/robots.txt":
        return True
    best: Rule | None = None
    for rule in group_for(robots_txt, product_token).rules:
        if not rule.matches(path):
            continue
        if best is None or rule.priority > best.priority or (rule.priority == best.priority and rule.allow):
            best = rule
    return best is None or best.allow


def crawl_delay(robots_txt: str, product_token: str) -> float:
    return group_for(robots_txt, product_token).crawl_delay or 0


@lru_cache(maxsize=256)
def group_for(robots_txt: str, product_token: str) -> Group:
    """The rules for the product token: its own groups merged, else those of "*", else none."""
    groups = _groups(robots_txt)
    own = groups.get(product_token.lower()) or groups.get("*")
    return own or Group()


def _groups(robots_txt: str) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    agents: list[str] = []  # the user agents of the group being read
    in_rules = False  # a rule ends the user-agent lines of a group; the next user-agent line starts a new one
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            # The product token only: "Googlebot/2.1" is "googlebot".
            agents.append(value.split("/")[0].strip().lower())
        elif key in ("allow", "disallow", "crawl-delay") and agents:
            in_rules = True
            for agent in agents:
                group = groups.setdefault(agent, Group())
                if key == "crawl-delay":
                    delay = _seconds(value)
                    group.crawl_delay = delay if delay is not None else group.crawl_delay
                elif value:  # an empty Disallow allows everything: no rule
                    group.rules.append(Rule(key == "allow", normalise(value)))
    return groups


def pattern_matches(pattern: str, path: str) -> bool:
    """Whether a normalised path pattern - `*` any characters, a trailing `$` the end - matches from the start of the
    path. Also the location syntax of TDMRep (tdm_policy)."""
    return _regex(pattern).match(path) is not None


def path_of(url: str) -> str:
    """The path and query of a URL, normalised as patterns are."""
    parts = urlsplit(url)
    path = normalise(parts.path or "/")
    return f"{path}?{normalise(parts.query)}" if parts.query else path


def normalise(text: str) -> str:
    return quote(unquote(text), safe=_SAFE)


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + (r"\Z" if anchored else ""))


def _seconds(value: str) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
