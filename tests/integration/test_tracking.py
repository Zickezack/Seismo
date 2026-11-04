"""Tracked Wertpapiere in PostgreSQL (userstories.md, Live-Kurse und Kursdatenspeicher, story 2): collected every
minute from a simulated SIX, Alpaca's minutes page by page, a time not collected kept as a gap, and full resolution
compressed to hours - never deleted without its replacement."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from sqlalchemy import insert, select, text, update

from app.db.models import Instrument, Job, PriceGap, PricePoint
from app.jobs.handlers import build_handlers
from app.jobs.runner import run_next_job
from app.repositories import price_repository
from app.services import instrument_service, job_service, platform_settings_service, price_service, tracking_service
from app.sources.http import HttpClient
from tests.integration.test_duplicates import database  # noqa: F401
from tests.integration.test_prices import NESTLE, Market, instrument_row, listed

COLLECT, MINUTES = job_service.COLLECT_PRICES_JOB_KIND, job_service.FETCH_MINUTES_JOB_KIND
# A Wednesday, during SIX's trading hours (10:00 in Zurich).
TRADING = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


class Minutes(Market):
    """Market, and Alpaca's minutes: `minutes` (symbol: [(time, close)]), `page` of them per answer."""

    def __init__(self):
        super().__init__()
        self.minutes, self.page, self.asked = {}, 2, []

    def __call__(self, request):
        if request.url.path != "/v2/stocks/bars":
            return super().__call__(request)
        query = parse_qs(urlsplit(str(request.url)).query)
        self.asked.append(query)
        start, end = (datetime.fromisoformat(query[key][0].replace("Z", "+00:00")) for key in ("start", "end"))
        # As Alpaca's free plan: SIP's last 15 minutes are not given.
        if end > datetime.now(UTC) - timedelta(minutes=15):
            return httpx2.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
        found = [(symbol, at, close) for symbol in query["symbols"][0].split(",") for at, close in self.minutes.get(symbol, [])
                 if start <= at < end]
        offset = int(query.get("page_token", ["0"])[0])
        shown, rest = found[offset : offset + self.page], found[offset + self.page :]
        bars: dict = {}
        for symbol, at, close in shown:
            bars.setdefault(symbol, []).append({"t": at.isoformat(), "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 10})
        return httpx2.Response(200, json={"bars": bars, "next_page_token": str(offset + self.page) if rest else None})


@pytest.fixture
def market(monkeypatch):
    fake = Minutes()
    monkeypatch.setenv("ALPACA_API_KEY_ID", "alpaca-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "alpaca-secret")
    client = lambda contact: HttpClient(contact, httpx2.MockTransport(fake))  # noqa: E731
    monkeypatch.setattr(price_service, "HttpClient", client)
    monkeypatch.setattr(tracking_service, "HttpClient", client)
    return fake


def nestle(database):
    return instrument_service.create(database, NESTLE | {"valor_id": NESTLE["valor_id"]}).id


def run_jobs(database, *kinds):
    """Run the queued jobs, each as if a minute later: the hosts free again, a job deferred to its slot due."""
    handlers = build_handlers(database, Path("/nonexistent"), None, None, "ops@example.org")
    while True:
        with database.begin() as connection:
            connection.execute(text("UPDATE fetch_host SET next_slot_at = now(), last_request_at = now() - interval '1 minute'"))
            connection.execute(text("UPDATE job SET run_after = now() WHERE status = 'queued'"))
        if not run_next_job(database, {kind: handlers[kind] for kind in kinds}, "test", 10, on_renewed=lambda: None):
            return


def collected_at(database, instrument_id, moment):
    with database.begin() as connection:
        connection.execute(update(Instrument).where(Instrument.id == instrument_id).values(collected_through=moment, last_error_at=None))


def points(database, resolution=None):
    statement = select(PricePoint.__table__).order_by(PricePoint.at)
    if resolution:
        statement = statement.where(PricePoint.resolution == resolution)
    with database.connect() as connection:
        return connection.execute(statement).all()


def gaps(database):
    with database.connect() as connection:
        return connection.execute(select(PriceGap.__table__).order_by(PriceGap.id)).all()


# Tracking on and off.

def test_tracking_starts_the_collection_now_and_ends_keeping_what_was_collected(database):
    instrument_id = nestle(database)
    assert tracking_service.set_tracked(database, [instrument_id], True).processed == 1
    row = instrument_row(database, instrument_id)
    assert row.tracked and row.tracked_since is not None and row.collected_through == row.tracked_since
    # Switched on again while on, nothing starts anew.
    tracking_service.set_tracked(database, [instrument_id], True)
    assert instrument_row(database, instrument_id).tracked_since == row.tracked_since
    tracking_service.set_tracked(database, [instrument_id], False)
    off = instrument_row(database, instrument_id)
    assert not off.tracked and off.tracked_since == row.tracked_since


def test_beyond_the_most_tracked_or_one_no_longer_traded_nothing_is_tracked(database):
    platform_settings_service.save(database, {"tracked_instruments_max": "2"})
    first, second, third = listed(database, "AAA"), listed(database, "BBB"), listed(database, "CCC")
    assert tracking_service.set_tracked(database, [first, second], True).processed == 2
    result = tracking_service.set_tracked(database, [third], True)
    assert result.processed == 0 and result.blocked == ["höchstens 2 verfolgte Wertpapiere (Plattform-Einstellungen), es wären 3"]
    with database.begin() as connection:
        connection.execute(update(Instrument).where(Instrument.id == third).values(status="inactive"))
    tracking_service.set_tracked(database, [first], False)
    # Every blocker named at once.
    result = tracking_service.set_tracked(database, [first, third], True)
    assert result.processed == 0
    assert result.blocked == ["CCC Inc. (nicht mehr gehandelt)", "höchstens 2 verfolgte Wertpapiere (Plattform-Einstellungen), es wären 3"]
    assert not instrument_row(database, first).tracked


# Collection.

def test_a_tracked_wertpapier_of_six_is_collected_every_minute_instead_of_its_live_fetch(database, market):
    instrument_id = nestle(database)
    tracking_service.set_tracked(database, [instrument_id], True)
    collected_at(database, instrument_id, datetime.now(UTC) - timedelta(minutes=2))
    market.prices[NESTLE["valor_id"]] = (TRADING, 81.5)

    assert len(tracking_service.enqueue_due(database)) == 1
    # Its Live-Kurs comes from the collection: no job of its own.
    assert price_service.enqueue_due(database) == []
    run_jobs(database, COLLECT)

    [point] = points(database)
    assert (point.resolution, point.close, point.source, point.delayed) == ("quote", Decimal("81.500000"), "six", True)
    row = instrument_row(database, instrument_id)
    assert row.collected_through > datetime.now(UTC) - timedelta(minutes=1) and row.last_fetched_at is not None
    # Collected within the minute: not due again.
    assert tracking_service.enqueue_due(database) == []
    assert gaps(database) == []


def test_a_time_not_collected_during_trading_hours_is_a_gap_never_filled(database, market, monkeypatch):
    instrument_id = nestle(database)
    tracking_service.set_tracked(database, [instrument_id], True)
    market.prices[NESTLE["valor_id"]] = (TRADING, 81.5)
    # The worker was away for twenty minutes of trading.
    collected_at(database, instrument_id, TRADING - timedelta(minutes=20))
    monkeypatch.setattr(tracking_service, "datetime", _Clock(TRADING))
    tracking_service.enqueue_due(database, now=TRADING)
    run_jobs(database, COLLECT)

    [gap] = gaps(database)
    assert (gap.starts_at, gap.ends_at, gap.cause) == (TRADING - timedelta(minutes=20), TRADING, "not_asked")
    assert len(points(database)) == 1  # nothing put into it


def test_a_source_failing_makes_its_gap_the_source_s(database, market, monkeypatch):
    instrument_id = nestle(database)
    tracking_service.set_tracked(database, [instrument_id], True)
    collected_at(database, instrument_id, TRADING - timedelta(minutes=10))
    market.status = 503
    tracking_service.enqueue_due(database, now=TRADING)
    run_jobs(database, COLLECT)
    assert instrument_row(database, instrument_id).collected_through == TRADING - timedelta(minutes=10)

    market.status, market.prices[NESTLE["valor_id"]] = 200, (TRADING, 81.5)
    with database.begin() as connection:
        connection.execute(text("UPDATE job SET status = 'failed' WHERE status = 'queued'"))
        connection.execute(update(Instrument).values(last_error_at=TRADING - timedelta(minutes=5)))
    monkeypatch.setattr(tracking_service, "datetime", _Clock(TRADING))
    tracking_service.enqueue_due(database, now=TRADING)
    run_jobs(database, COLLECT)
    [gap] = gaps(database)
    assert gap.cause == "source_error"


def test_outside_trading_hours_no_collection_is_no_gap(database, market, monkeypatch):
    instrument_id = nestle(database)
    tracking_service.set_tracked(database, [instrument_id], True)
    night = datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
    collected_at(database, instrument_id, night - timedelta(hours=5))
    market.prices[NESTLE["valor_id"]] = (TRADING, 81.5)
    monkeypatch.setattr(tracking_service, "datetime", _Clock(night))
    tracking_service.enqueue_due(database, now=night)
    run_jobs(database, COLLECT)
    assert gaps(database) == []


class _Clock:
    """datetime with now() at a fixed moment, for the time a collection records."""

    def __init__(self, moment):
        self.moment = moment

    def now(self, tz=None):
        return self.moment

    def __getattr__(self, name):
        return getattr(datetime, name)


def test_alpaca_s_minutes_come_page_by_page_and_heal_a_time_the_worker_was_away(database, market):
    # The live chain alone: no history before tracking.
    platform_settings_service.save(database, {"minute_backfill_days": "0"})
    instrument_id = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [instrument_id], True)
    start = tracking_service.minutes_end(datetime.now(UTC)) - timedelta(hours=2)
    collected_at(database, instrument_id, start)
    market.minutes["AAPL"] = [(start + timedelta(minutes=index), 100 + index) for index in range(5)]

    assert len(tracking_service.enqueue_due(database)) == 1
    # One chain at a time.
    assert tracking_service.enqueue_due(database) == []
    run_jobs(database, MINUTES)

    minutes = points(database, "minute")
    assert [point.close for point in minutes] == [Decimal(100 + index) for index in range(5)]
    assert (minutes[0].open, minutes[0].high, minutes[0].low, minutes[0].volume, minutes[0].delayed) == (100, 101, 99, 10, True)
    # Three pages of two, from where the collection came up to 16 minutes ago.
    assert len(market.asked) == 3 and market.asked[0]["feed"] == ["sip"] and market.asked[0]["timeframe"] == ["1Min"]
    end = tracking_service.minutes_end(datetime.now(UTC))
    assert abs(instrument_row(database, instrument_id).collected_through - end) < timedelta(minutes=2)
    assert gaps(database) == []


def test_the_collection_page_shows_gaps_counts_and_the_latest_minutes(database, market):
    instrument_id = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [instrument_id], True)
    with database.begin() as connection:
        connection.execute(insert(PriceGap).values(instrument_id=instrument_id, starts_at=TRADING, ends_at=TRADING + timedelta(minutes=30), cause="source_error"))
        price_repository.insert_prices(connection, [minute(instrument_id, TRADING)])
    detail = instrument_service.detail(database, instrument_id)
    collection = detail.collection
    assert detail.instrument.tracked and collection.counts == {"minute": 1} and collection.gap_count == 1
    assert [gap.cause_label for gap in collection.gaps] == ["Quelle fehlerhaft"] and len(collection.minutes) == 1
    assert collection.full_resolution_days == 90


# Compression.

def minute(instrument_id, at, close=100, received_at=None, resolution="minute", volume=10):
    return {"instrument_id": instrument_id, "resolution": resolution, "at": at, "open": close - 1, "high": close + 2, "low": close - 2,
            "close": close, "volume": volume, "previous_close": None, "source": "alpaca", "delayed": True,
            "received_at": received_at or datetime.now(UTC)}


def test_minutes_older_than_the_period_become_hours_and_none_goes_without_its_replacement(database):
    instrument_id = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [instrument_id], True)
    old = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=100)
    recent = datetime.now(UTC) - timedelta(days=1)
    with database.begin() as connection:
        price_repository.insert_prices(connection, [
            minute(instrument_id, old + timedelta(minutes=5), 100, received_at=old + timedelta(minutes=30)),
            minute(instrument_id, old + timedelta(minutes=10), 103, received_at=old + timedelta(minutes=40)),
            minute(instrument_id, old + timedelta(minutes=59), 101, received_at=old + timedelta(hours=2)),
            minute(instrument_id, old + timedelta(hours=1, minutes=1), 105),
            minute(instrument_id, recent, 110),
        ])

    result = tracking_service.compress_due(database)

    assert (result.points, result.hours) == (4, 2)
    [first, second] = points(database, "hour")
    assert (first.at, first.open, first.high, first.low, first.close, first.volume, first.samples) == (old, 99, 105, 98, 101, 30, 3)
    # Known when the last of its minutes was (datamodel.md, rule 11).
    assert first.received_at == old + timedelta(hours=2)
    assert (second.at, second.samples) == (old + timedelta(hours=1), 1)
    assert [point.at for point in points(database, "minute")] == [recent]
    assert tracking_service.compress_due(database).points == 0


def test_quotes_become_hours_of_their_prices_with_the_volume_unknown(database):
    instrument_id = nestle(database)
    old = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=100)
    with database.begin() as connection:
        price_repository.insert_prices(connection, [
            minute(instrument_id, old + timedelta(minutes=1), 80, resolution="quote", volume=5000) | {"source": "six"},
            minute(instrument_id, old + timedelta(minutes=2), 82, resolution="quote", volume=6000) | {"source": "six"},
        ])
    tracking_service.compress_due(database)
    [hour] = points(database, "hour")
    assert (hour.open, hour.high, hour.low, hour.close, hour.volume, hour.source) == (80, 82, 80, 82, None, "six")


def test_a_late_point_of_an_hour_compressed_already_stays_as_it_is(database):
    instrument_id = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [instrument_id], True)
    old = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=100)
    with database.begin() as connection:
        price_repository.insert_prices(connection, [minute(instrument_id, old + timedelta(minutes=1))])
    tracking_service.compress_due(database)
    with database.begin() as connection:
        price_repository.insert_prices(connection, [minute(instrument_id, old + timedelta(minutes=2))])
    assert tracking_service.compress_due(database).points == 0
    assert len(points(database, "minute")) == 1 and len(points(database, "hour")) == 1


def test_unlimited_full_resolution_and_another_compressing_worker_compress_nothing(database):
    instrument_id = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [instrument_id], True)
    old = datetime.now(UTC) - timedelta(days=400)
    with database.begin() as connection:
        price_repository.insert_prices(connection, [minute(instrument_id, old)])
    platform_settings_service.save(database, {"price_full_resolution_days": "0"})
    assert tracking_service.compress_due(database).points == 0
    platform_settings_service.save(database, {"price_full_resolution_days": "90"})
    with database.connect() as other:
        with other.begin():
            assert price_repository.try_lock_compression(other)
            assert tracking_service.compress_due(database).points == 0
    assert tracking_service.compress_due(database).points == 1


def test_the_need_per_tracked_wertpapier_and_day_is_measured(database):
    instrument_id = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [instrument_id], True)
    with database.begin() as connection:
        connection.execute(update(Instrument).values(tracked_since=text("now() - interval '2 days'")))
        price_repository.insert_prices(connection, [minute(instrument_id, datetime.now(UTC) - timedelta(hours=hour)) for hour in range(1, 41)])
    with database.connect() as connection:
        need = tracking_service.need(connection)
    assert need.tracked == 1 and round(need.measured_days) == 2 and round(need.points_per_day) == 20
    with database.connect() as connection:
        assert connection.execute(select(Job).where(Job.kind == MINUTES)).first() is None



# The minutes from before tracking began (Alpaca's SIP history).

def test_switched_on_alpaca_s_minutes_of_the_days_before_are_to_be_fetched_and_none_of_six(database):
    apple, smi = listed(database, "AAPL", live=False), nestle(database)
    tracking_service.set_tracked(database, [apple, smi], True)
    row = instrument_row(database, apple)
    assert row.backfill_through == row.tracked_since - timedelta(days=90)
    assert instrument_row(database, smi).backfill_through is None
    platform_settings_service.save(database, {"minute_backfill_days": "0"})
    other = listed(database, "MSFT", live=False)
    tracking_service.set_tracked(database, [other], True)
    row = instrument_row(database, other)
    assert row.backfill_through == row.tracked_since


def test_the_minutes_before_tracking_come_week_by_week_behind_live_work_for_those_switched_on_together(database, market):
    platform_settings_service.save(database, {"minute_backfill_days": "10"})
    apple, microsoft = listed(database, "AAPL", live=False), listed(database, "MSFT", live=False)
    tracking_service.set_tracked(database, [apple, microsoft], True)
    since = instrument_row(database, apple).tracked_since
    market.minutes["AAPL"] = [(since - timedelta(days=days, minutes=20), 100 + days) for days in range(10)]
    market.minutes["MSFT"] = [(since - timedelta(days=9, hours=5), 400)]

    queued = tracking_service.enqueue_due(database)
    with database.connect() as connection:
        kinds = dict(connection.execute(select(Job.kind, Job.priority).where(Job.id.in_(queued))).all())
    assert kinds[job_service.BACKFILL_MINUTES_JOB_KIND] == 1
    run_jobs(database, job_service.BACKFILL_MINUTES_JOB_KIND)

    assert sorted(point.close for point in points(database, "minute")) == sorted([Decimal(100 + days) for days in range(10)] + [Decimal(400)])
    # Both in each request; two steps of a week at most, each page by page.
    assert all(query["symbols"] == ["AAPL,MSFT"] for query in market.asked)
    starts = sorted({query["start"][0] for query in market.asked})
    assert len(starts) == 2
    # Up to 16 minutes ago: the last ones before tracking began come once they are free.
    for instrument_id in (apple, microsoft):
        row = instrument_row(database, instrument_id)
        assert row.backfill_through == tracking_service.minutes_end(datetime.now(UTC)) or row.backfill_through < row.tracked_since
    with database.begin() as connection:
        # Twenty minutes later: everything twenty minutes further back.
        connection.execute(update(Instrument).values(
            tracked_since=text("tracked_since - interval '20 minutes'"), backfill_through=text("backfill_through - interval '20 minutes'"),
        ))
    tracking_service.enqueue_due(database)
    run_jobs(database, job_service.BACKFILL_MINUTES_JOB_KIND)
    for instrument_id in (apple, microsoft):
        row = instrument_row(database, instrument_id)
        assert row.backfill_through == row.tracked_since
    # Done: nothing more to start.
    assert job_service.BACKFILL_MINUTES_JOB_KIND not in [
        kind for kind, in _kinds(database, tracking_service.enqueue_due(database))
    ]


def _kinds(database, job_ids):
    with database.connect() as connection:
        return connection.execute(select(Job.kind).where(Job.id.in_(job_ids))).all()


def test_a_wertpapier_no_longer_tracked_gets_no_more_of_its_history(database, market):
    apple = listed(database, "AAPL", live=False)
    tracking_service.set_tracked(database, [apple], True)
    tracking_service.set_tracked(database, [apple], False)
    assert job_service.BACKFILL_MINUTES_JOB_KIND not in [kind for kind, in _kinds(database, tracking_service.enqueue_due(database))]
