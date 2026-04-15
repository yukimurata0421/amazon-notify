import json
import threading
import time
from pathlib import Path

from amazon_notify.service_status import ServiceStatusTracker


def test_service_status_writes_expected_schema(tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "runtime" / "pubsub-heartbeat.json"
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text(
        json.dumps({"schema_version": 1, "updated_at": 1_700_000_000.0}),
        encoding="utf-8",
    )
    status_file = tmp_path / "runtime" / "status.json"

    tracker = ServiceStatusTracker(
        status_file=status_file, heartbeat_file=heartbeat_file
    )
    tracker.mark_trigger_result(True, reason="run_once_failed")

    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["service"] == "amazon-notify"
    assert payload["internal_state"] == "healthy"
    assert payload["reason"] == "running"
    assert payload["updated_at"]
    assert payload["last_success_ts"]
    assert payload["last_progress_ts"]
    assert payload["recovery"]["consecutive_failures"] == 0
    assert payload["components"]["pubsub"]["status"] == "healthy"
    assert payload["components"]["pubsub"]["last_heartbeat_ts"]
    assert payload["components"]["gmail"]["last_success_ts"]
    assert payload["components"]["discord"]["last_success_ts"]
    assert payload["components"]["checkpoint"]["last_advanced_ts"]


def test_service_status_updates_are_atomic_for_readers(tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "runtime" / "pubsub-heartbeat.json"
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text(
        json.dumps({"schema_version": 1, "updated_at": 1_700_000_000.0}),
        encoding="utf-8",
    )
    status_file = tmp_path / "runtime" / "status.json"

    tracker = ServiceStatusTracker(
        status_file=status_file, heartbeat_file=heartbeat_file
    )
    failures: list[str] = []

    def writer() -> None:
        for _ in range(120):
            tracker.mark_reconnect_attempt(
                action="stream_recycle",
                reason="pubsub_stream_session_failed",
            )
            tracker.mark_trigger_result(True, reason="run_once_failed")

    thread = threading.Thread(target=writer)
    thread.start()

    while thread.is_alive():
        if status_file.exists():
            try:
                json.loads(status_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures.append(str(exc))
                break

    thread.join(timeout=2.0)

    assert failures == []
    assert not list(status_file.parent.glob(f".{status_file.name}.*.tmp"))


def test_service_status_mark_system_progress_updates_last_progress(
    tmp_path: Path,
) -> None:
    heartbeat_file = tmp_path / "runtime" / "pubsub-heartbeat.json"
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text(
        json.dumps({"schema_version": 1, "updated_at": 1_700_000_000.0}),
        encoding="utf-8",
    )
    status_file = tmp_path / "runtime" / "status.json"
    tracker = ServiceStatusTracker(
        status_file=status_file, heartbeat_file=heartbeat_file
    )

    tracker.mark_trigger_result(True, reason="run_once_failed")
    before = json.loads(status_file.read_text(encoding="utf-8"))["last_progress_ts"]
    time.sleep(0.01)
    tracker.mark_system_progress(reason="heartbeat_tick")
    after = json.loads(status_file.read_text(encoding="utf-8"))["last_progress_ts"]

    assert before is not None
    assert after is not None
    assert after >= before
