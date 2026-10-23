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
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from sqlalchemy import Engine, Row

from app.db.models import RobotsStatus
from app.repositories import fetch_host_repository
from app.sources.errors import FetchDisallowed, HostBusy, SourceRejected, SourceUnavailable
from app.sources.http import HttpClient, HttpResponse, unavailable

ROBOTS_MAX_AGE = timedelta(hours=24)
# RFC 9309 asks crawlers to read at least 500 KiB of a robots.txt.
ROBOTS_MAX_BYTES = 500 * 1024
# The product token robots.txt groups are matched against ("User-agent: Seismo").
USER_AGENT_TOKEN = "Seismo"
# A reserved slot this close counts as now; deferring the job would take longer.
SLOT_TOLERANCE = timedelta(seconds=1)
# A longer Retry-After is taken as this: a mistake rather than a wish, and a feed paused for days loses items.
MAX_PAUSE = timedelta(hours=24)


@dataclass(frozen=True)
class RobotsRules:
    status: RobotsStatus
    robots_txt: str | None = None

    def _parser(self) -> RobotFileParser:
        parser = RobotFileParser()
        parser.parse((self.robots_txt or "").splitlines())
        return parser

    def allows(self, url: str) -> bool:
        if self.status == RobotsStatus.ALLOW_ALL:
            return True
        if self.status == RobotsStatus.DISALLOW_ALL:
            return False
        return self._parser().can_fetch(USER_AGENT_TOKEN, url)

    def crawl_delay(self) -> float:
        if self.status != RobotsStatus.RULES:
            return 0
        return float(self._parser().crawl_delay(USER_AGENT_TOKEN) or 0)


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
    return RobotsRules(RobotsStatus.RULES, response.content.decode("utf-8", errors="replace"))


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

    def get(self, url: str, accept: str) -> HttpResponse:
        """Raises FetchDisallowed, HostBusy, or what HttpClient.get raises."""
        host, robots_url = _host_of(url)
        with self._engine.connect() as connection:
            known = fetch_host_repository.get(connection, host, ROBOTS_MAX_AGE)
        if known is not None and known.paused:
            if self._backfill:
                with self._engine.connect() as connection:
                    raise HostBusy(fetch_host_repository.free_at_of(connection, host, self._min_interval), reserved=False)
            # A slot reserved before the pause began is void; the new one lies after it.
            with self._engine.begin() as connection:
                slot, _now = fetch_host_repository.reserve_slot(connection, host, self._min_interval)
            raise HostBusy(slot)
        try:
            rules = self._robots(host, robots_url, known)
            if not rules.allows(url):
                raise FetchDisallowed("Durch robots.txt gesperrt")
            self._wait_for_turn(host, max(self._min_interval, rules.crawl_delay()))
            return self._http.get(url, accept)
        except SourceUnavailable as error:
            if error.retry_after:
                with self._engine.begin() as connection:
                    fetch_host_repository.pause(connection, host, min(error.retry_after, MAX_PAUSE.total_seconds()))
            raise

    def _robots(self, host: str, robots_url: str, known: Row | None) -> RobotsRules:
        if known is not None and known.robots_fresh:
            return RobotsRules(RobotsStatus(known.robots_status), known.robots_txt)
        rules = robots_rules(self._http.fetch(robots_url, "text/plain", ROBOTS_MAX_BYTES))
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
