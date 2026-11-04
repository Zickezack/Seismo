"""The rules of tracking without a database (userstories.md, Live-Kurse und Kursdatenspeicher, story 2): what counts
as trading time, when a time without a collection is a gap and whose, and up to where Alpaca's minutes are free."""
from datetime import UTC, datetime, timedelta

import pytest

from app.services import tracking_service
from app.services.tracking_service import cause, is_gap, minutes_end, trading_time

# Wednesday 4 November 2026: SIX trades 9:00 to 17:30 in Zurich, 8:00 to 16:30 UTC (winter time).
WEDNESDAY = datetime(2026, 11, 4, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.mark.parametrize(("start", "end", "expected"), [
    (WEDNESDAY + 9 * HOUR, WEDNESDAY + 10 * HOUR, HOUR),  # within
    (WEDNESDAY + 6 * HOUR, WEDNESDAY + 9 * HOUR, HOUR),  # opens at 8:00 UTC
    (WEDNESDAY + 16 * HOUR, WEDNESDAY + 20 * HOUR, HOUR / 2),  # closes at 16:30 UTC
    (WEDNESDAY + 17 * HOUR, WEDNESDAY + 31 * HOUR, timedelta()),  # the night
    (WEDNESDAY + 2 * 24 * HOUR, WEDNESDAY + 5 * 24 * HOUR, 8.5 * HOUR),  # Friday to Monday night: Friday only
])
def test_six_trades_on_weekdays_from_nine_to_half_past_five_in_zurich(start, end, expected):
    assert trading_time("six", start, end) == expected


def test_in_summer_six_s_hours_are_an_hour_earlier_in_utc():
    june = datetime(2026, 6, 3, tzinfo=UTC)
    assert trading_time("six", june + 6 * HOUR, june + 8 * HOUR) == HOUR


def test_every_hour_counts_for_eodhd_and_nothing_backwards():
    assert trading_time("eodhd", WEDNESDAY, WEDNESDAY + 3 * HOUR) == 3 * HOUR
    assert trading_time("six", WEDNESDAY + 10 * HOUR, WEDNESDAY + 9 * HOUR) == timedelta()


def test_a_gap_is_more_than_three_minutes_of_trading_without_a_collection():
    opening = WEDNESDAY + 8 * HOUR
    assert not is_gap("six", opening, opening + timedelta(minutes=3))
    assert is_gap("six", opening, opening + timedelta(minutes=3, seconds=1))
    # Three hours of night and four minutes of trading after the opening.
    assert is_gap("six", opening - 3 * HOUR, opening + timedelta(minutes=4))
    assert not is_gap("six", opening - 3 * HOUR, opening + timedelta(minutes=2))
    assert not is_gap("six", None, opening)


def test_a_gap_is_the_source_s_when_it_failed_since_the_last_collection():
    last = WEDNESDAY + 9 * HOUR
    assert cause(last, last + timedelta(minutes=5)) == "source_error"
    assert cause(last, last - timedelta(minutes=5)) == "not_asked"
    assert cause(last, None) == "not_asked"


def test_alpaca_s_minutes_are_asked_for_up_to_sixteen_minutes_ago_by_the_whole_minute():
    assert minutes_end(datetime(2026, 11, 4, 15, 0, 42, tzinfo=UTC)) == datetime(2026, 11, 4, 14, 44, tzinfo=UTC)


def test_the_need_of_all_tracked_together():
    need = tracking_service.Need(tracked=4, points_per_day=500, measured_days=7, full_resolution_days=90)
    assert need.bytes_per_day(100) == 200_000
