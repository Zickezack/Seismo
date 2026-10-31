"""Fetch policy for every request to a source's host, feed or article: robots.txt first, then at most one
request per interval (userstories.md, Quellenadapter und Ingestion, story 3).

The interval is the Plattform-Einstellung "Mindestabstand pro Domain", or the host's Crawl-delay when that
is longer. A request whose slot lies ahead raises HostBusy with the slot reserved for it; its job is deferred
to that slot and passes it back as `reserved_slot` (app/jobs/handlers.py), so a busy host never blocks the
worker and each waiting request gets a slot of its own.

A host that answers with Retry-After (e.g. 429, 503) is paused that long, at most a day: no request goes there
before - robots.txt included - and every job for it is deferred to a slot after the pause (story 4).

A backfill request (story 6) reserves no slot: it goes only while the host is free and otherwise comes back when
it is, so live requests always get the next slot.
"""
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import Engine, Row

from app.db.models import RobotsStatus, TdmrepStatus
from app.repositories import fetch_host_repository
from app.services import robots_txt, tdm_policy
from app.sources.errors import FetchDisallowed, HostBusy, SourceRejected, SourceUnavailable
from app.sources.http import HttpClient, HttpResponse, unavailable

# How long a host's robots.txt and tdmrep.json count before they are asked for again.
ROBOTS_MAX_AGE = timedelta(hours=24)
# RFC 9309 asks crawlers to read at least 500 KiB of a robots.txt.
ROBOTS_MAX_BYTES = 500 * 1024
# The product token robots.txt groups are matched against ("User-agent: Seismo").
USER_AGENT_TOKEN = "Seismo"
# A reserved slot this close counts as now; deferring the job would take longer.
SLOT_TOLERANCE = timedelta(seconds=1)
# A longer Retry-After is taken as this: a mistake rather than a wish, and a feed paused for days loses items.
MAX_PAUSE = timedelta(hours=24)
# A host that answers "too many requests" (HTTP 429) without saying how long to wait is paused this long: each job of
# it retrying on its own keeps a throttle going (GDELT, measured 2026-10-31: six queries and their retries kept being
# refused, even a single request a minute and a half after the last).
RATE_LIMIT_PAUSE = timedelta(minutes=5)


@dataclass(frozen=True)
class RobotsRules:
    status: RobotsStatus
    robots_txt: str | None = None

    def allows(self, url: str) -> bool:
        if self.status == RobotsStatus.ALLOW_ALL:
            return True
        if self.status == RobotsStatus.DISALLOW_ALL:
            return False
        return robots_txt.allows(self.robots_txt or "", USER_AGENT_TOKEN, url)

    def crawl_delay(self) -> float:
        if self.status != RobotsStatus.RULES:
            return 0
        return robots_txt.crawl_delay(self.robots_txt or "", USER_AGENT_TOKEN)


def robots_rules(response: HttpResponse) -> RobotsRules:
    """How a robots.txt answer applies; raises SourceUnavailable when it should be asked again later.

    401 and 403 disallow everything (the admin's decision, more careful than RFC 9309); 404 and the other
    4xx mean there are no rules; 5xx and 429 say nothing yet.
    """
    if response.status in (401, 403):
        return RobotsRules(RobotsStatus.DISALLOW_ALL)
    if response.status >= 500 or response.status == 429:
        raise unavailable(response, "robots.txt: ")
    if response.status >= 400:
        return RobotsRules(RobotsStatus.ALLOW_ALL)
    content = response.content
    if response.cut:
        # RFC 9309, 2.5: read at least 500 KiB, and only that much; the line cut in two counts no more.
        content = content[: content.rfind(b"\n") + 1]
    return RobotsRules(RobotsStatus.RULES, content.decode("utf-8", errors="replace"))


class PoliteHttp:
    """An HttpClient for one job: asks robots.txt first and keeps the host's minimum interval."""

    def __init__(
        self,
        engine: Engine,
        http: HttpClient,
        min_interval_seconds: float,
        reserved_slot: datetime | None = None,
        backfill: bool = False,
    ) -> None:
        self._engine = engine
        self._http = http
        self._min_interval = min_interval_seconds
        self._reserved_slot = reserved_slot
        self._backfill = backfill

    def get(self, url: str, accept: str, permitted: bool = False) -> HttpResponse:
        """Raises FetchDisallowed, HostBusy, or what HttpClient.get raises.

        `permitted`: the publisher approved this access explicitly - the publisher's permission, an allowed route
        (architecture.md) - so robots.txt is not asked; ReliefWeb's API approves an appname and disallows every
        path in its robots.txt. The minimum interval and pauses still apply.
        """
        host, robots_url = _host_of(url)
        known = self._known_unless_paused(host)
        with self._pausing_on_retry_after(host):
            interval = self._min_interval
            if not permitted:
                rules = self._robots(host, robots_url, known)
                if not rules.allows(url):
                    raise FetchDisallowed("Durch robots.txt gesperrt")
                interval = max(interval, rules.crawl_delay())
            self._wait_for_turn(host, interval)
            return self._http.get(url, accept)

    def tdm_rules(self, url: str) -> str | None:
        """The rules of the host's /.well-known/tdmrep.json, asked for once a day like robots.txt (no request slot);
        None when the host has none. Raises HostBusy during a pause, SourceUnavailable to be asked again later."""
        host, _robots_url = _host_of(url)
        known = self._known_unless_paused(host)
        if known is not None and known.tdmrep_fresh:
            return known.tdmrep_rules
        scheme = urlsplit(url).scheme
        with self._pausing_on_retry_after(host):
            tdmrep_url = f"{scheme}://{host}{tdm_policy.TDMREP_PATH}"
            # Cut, not refused: many hosts answer any unknown path with a page of their own, often above the limit.
            response = self._http.fetch(tdmrep_url, "application/json", tdm_policy.TDMREP_MAX_BYTES, cut=True)
            if response.status >= 500 or response.status == 429:
                raise unavailable(response, "tdmrep.json: ")
        # No file, a 4xx, or a file that is no TDMRep - one cut at the limit is none: the host does not use the protocol.
        rules = tdm_policy.parse_tdmrep(response.content) if response.status == 200 and not response.cut else None
        with self._engine.begin() as connection:
            status = TdmrepStatus.RULES if rules is not None else TdmrepStatus.NONE
            fetch_host_repository.save_tdmrep(connection, host, status, rules)
        return rules

    def _known_unless_paused(self, host: str) -> Row | None:
        """What is known of the host; HostBusy while it asked for a pause (Retry-After)."""
        with self._engine.connect() as connection:
            known = fetch_host_repository.get(connection, host, ROBOTS_MAX_AGE)
        if known is None or not known.paused:
            return known
        if self._backfill:
            with self._engine.connect() as connection:
                raise HostBusy(fetch_host_repository.free_at_of(connection, host, self._min_interval), reserved=False)
        # A slot reserved before the pause began is void; the new one lies after it.
        with self._engine.begin() as connection:
            slot, _now = fetch_host_repository.reserve_slot(connection, host, self._min_interval)
        raise HostBusy(slot)

    @contextmanager
    def _pausing_on_retry_after(self, host: str) -> Iterator[None]:
        try:
            yield
        except SourceUnavailable as error:
            pause = error.retry_after or (RATE_LIMIT_PAUSE.total_seconds() if error.rate_limited else None)
            if pause:
                with self._engine.begin() as connection:
                    fetch_host_repository.pause(connection, host, min(pause, MAX_PAUSE.total_seconds()))
            raise

    def _robots(self, host: str, robots_url: str, known: Row | None) -> RobotsRules:
        if known is not None and known.robots_fresh:
            return RobotsRules(RobotsStatus(known.robots_status), known.robots_txt)
        rules = robots_rules(self._http.fetch(robots_url, "text/plain", ROBOTS_MAX_BYTES, cut=True))
        with self._engine.begin() as connection:
            fetch_host_repository.save_robots(connection, host, rules.status, rules.robots_txt)
        return rules

    def _take_free_slot(self, host: str, interval: float) -> None:
        with self._engine.begin() as connection:
            free_at = fetch_host_repository.take_free_slot(connection, host, interval)
        if free_at is not None:
            raise HostBusy(free_at, reserved=False)

    def _wait_for_turn(self, host: str, interval: float) -> None:
        if self._backfill:
            self._take_free_slot(host, interval)
            return
        if self._reserved_slot is None:
            with self._engine.begin() as connection:
                slot, now = fetch_host_repository.reserve_slot(connection, host, interval)
            if slot - now > SLOT_TOLERANCE:
                raise HostBusy(slot)
        # A slot is used once; a further request of the same job reserves its own.
        self._reserved_slot = None
        while True:
            with self._engine.begin() as connection:
                last_request_at, now = fetch_host_repository.lock_last_request(connection, host)
                wait = (last_request_at + timedelta(seconds=interval) - now).total_seconds() if last_request_at else 0
                if wait <= 0:
                    fetch_host_repository.mark_request(connection, host)
                    return
            # The job ran later than its slot (the worker was busy) and close to the previous request:
            # the minimum interval holds anyway. At most one interval.
            time.sleep(wait)


def _host_of(url: str) -> tuple[str, str]:
    """The host key (lower case, with a non-default port) and the URL of its robots.txt."""
    parts = urlsplit(url)
    if not parts.hostname:
        raise SourceRejected("Ungültige URL")
    host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return host, f"{parts.scheme}://{host}/robots.txt"
