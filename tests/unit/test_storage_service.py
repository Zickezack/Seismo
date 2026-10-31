"""The storage's growth from the daily measurements, without a database (app/services/storage_service.py)."""
from datetime import date, timedelta

from app.services import storage_service
from app.services.storage_service import Measurement
from app.web.templating import _size

DAY = date(2026, 10, 31)


def measured(days_ago, database_bytes, raw_text_bytes=0, articles=0):
    return Measurement(DAY - timedelta(days=days_ago), database_bytes, articles, 0, raw_text_bytes, 0, None)


def test_the_growth_per_day_is_measured_from_the_day_the_period_reaches_back_to():
    history = [measured(0, 1700, 700, 70), measured(3, 1400, 400, 40), measured(7, 1000, 0, 0), measured(9, 800, 0, 0)]

    growth = storage_service.growth(history, 7)

    assert (growth.days, growth.database_bytes, growth.raw_text_bytes, growth.articles) == (7, 100, 100, 10)


def test_while_the_measurements_reach_less_far_back_the_earliest_counts():
    growth = storage_service.growth([measured(0, 1300), measured(2, 1000)], 30)

    assert (growth.period, growth.days, growth.database_bytes) == (30, 2, 150)


def test_one_measurement_shows_no_growth_and_a_shrinking_one_a_negative():
    assert storage_service.growth([measured(0, 1000)], 7) is None
    assert storage_service.growth([measured(0, 900), measured(1, 1000)], 7).database_bytes == -100


def test_a_size_is_shown_in_steps_of_1024_with_one_decimal_below_ten():
    assert [_size(value) for value in (None, 0, 512, 1024, 3_355_443, 297_795_584, 1_181_116_006, -3_355_443, -0.2)] == [
        "–", "0 B", "512 B", "1,0 kB", "3,2 MB", "284 MB", "1,1 GB", "-3,2 MB", "0 B",
    ]
