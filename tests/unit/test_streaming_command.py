from amazon_notify.commands.streaming import _should_mark_progress_from_snapshot


def test_should_mark_progress_only_when_snapshot_advances_and_is_fresh() -> None:
    snapshot = {
        "updated_at": 100.0,
        "worker_last_seen_at": 100.0,
    }
    should_mark, updated = _should_mark_progress_from_snapshot(
        snapshot,
        previous_updated_at=None,
        now_ts=105.0,
        max_age_seconds=10.0,
    )
    assert should_mark
    assert updated == 100.0

    should_mark_same, updated_same = _should_mark_progress_from_snapshot(
        snapshot,
        previous_updated_at=100.0,
        now_ts=106.0,
        max_age_seconds=10.0,
    )
    assert not should_mark_same
    assert updated_same == 100.0


def test_should_not_mark_when_snapshot_or_worker_is_stale_or_invalid() -> None:
    stale_updated = {
        "updated_at": 100.0,
        "worker_last_seen_at": 100.0,
    }
    should_mark_stale, _ = _should_mark_progress_from_snapshot(
        stale_updated,
        previous_updated_at=None,
        now_ts=130.0,
        max_age_seconds=10.0,
    )
    assert not should_mark_stale

    stale_worker = {
        "updated_at": 120.0,
        "worker_last_seen_at": 90.0,
    }
    should_mark_worker_stale, _ = _should_mark_progress_from_snapshot(
        stale_worker,
        previous_updated_at=None,
        now_ts=130.0,
        max_age_seconds=10.0,
    )
    assert not should_mark_worker_stale

    invalid = {"updated_at": "bad"}
    should_mark_invalid, _ = _should_mark_progress_from_snapshot(
        invalid,
        previous_updated_at=None,
        now_ts=130.0,
        max_age_seconds=10.0,
    )
    assert not should_mark_invalid
