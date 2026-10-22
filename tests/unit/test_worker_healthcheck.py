import os

from app.worker.healthcheck import is_heartbeat_fresh


def test_missing_heartbeat_is_not_fresh(tmp_path):
    assert not is_heartbeat_fresh(tmp_path / "heartbeat", max_age_seconds=15)


def test_recent_heartbeat_is_fresh(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    os.utime(heartbeat, (1000, 1000))

    assert is_heartbeat_fresh(heartbeat, max_age_seconds=15, now=1015)


def test_old_heartbeat_is_not_fresh(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    os.utime(heartbeat, (1000, 1000))

    assert not is_heartbeat_fresh(heartbeat, max_age_seconds=15, now=1016)
