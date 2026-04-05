import errno
import json
from pathlib import Path

import pytest

from amazon_notify.checkpoint_store import JsonlCheckpointStore
from amazon_notify.domain import Checkpoint, RunResult
from amazon_notify.errors import CheckpointError


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_bootstrap_from_state_when_events_are_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "legacy-id"}), encoding="utf-8")
    events_file = tmp_path / "events.jsonl"

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)
    checkpoint = store.load_checkpoint()

    assert checkpoint.message_id == "legacy-id"
    events = _read_jsonl(events_file)
    assert len(events) == 1
    assert events[0]["event"] == "checkpoint_advanced"
    assert events[0]["bootstrap"] is True
    assert events[0]["source"] == "state_snapshot"


def test_events_jsonl_is_source_of_truth(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "state-id"}), encoding="utf-8")
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r1",
                "at": "2026-04-04 00:00:00",
                "checkpoint": "event-id",
                "source": "pipeline_commit",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)
    checkpoint = store.load_checkpoint()
    assert checkpoint.message_id == "event-id"


def test_ignores_corrupted_tail_line_in_jsonl(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "state-id"}), encoding="utf-8")
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"r1","at":"2026-04-04 00:00:00","checkpoint":"event-id"}\n'
        '{"broken":',
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)
    checkpoint = store.load_checkpoint()
    assert checkpoint.message_id == "event-id"


def test_raises_for_corrupted_middle_line_in_jsonl(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "state-id"}), encoding="utf-8")
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"r1","at":"2026-04-04 00:00:00","checkpoint":"event-id"}\n'
        '{"broken":\n'
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"r2","at":"2026-04-04 00:01:00","checkpoint":"event-id-2"}\n',
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)
    with pytest.raises(CheckpointError, match="途中行が破損"):
        store.load_checkpoint()


def test_append_run_result_includes_schema_version(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "x"}), encoding="utf-8")
    runs_file = tmp_path / "runs.jsonl"
    store = JsonlCheckpointStore(state_file=state_file, runs_file=runs_file)

    result = RunResult(
        run_id="run-1",
        started_at="2026-04-04 00:00:00",
        ended_at="2026-04-04 00:00:01",
        checkpoint_before="a",
        checkpoint_after="b",
        processed_count=1,
        matched_count=1,
        notified_count=1,
        non_target_count=0,
        failure_kind=None,
        failure_message=None,
        failure_message_id=None,
        should_retry=False,
        should_alert=False,
        auth_status=None,
    )
    store.append_run_result(result)

    rows = _read_jsonl(runs_file)
    assert rows
    assert rows[0]["schema_version"] == 1


def test_load_last_run_summary_and_incident_state(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "last_message_id": "x",
                "active_incident_kind": "delivery_failed",
                "active_incident_message": "failed",
                "active_incident_at": "2026-04-04 01:00:00",
                "incident_suppressed_count": 2,
            }
        ),
        encoding="utf-8",
    )
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "r1",
                "started_at": "2026-04-04 00:00:00",
                "ended_at": "2026-04-04 00:00:01",
                "checkpoint_before": "a",
                "checkpoint_after": "b",
                "processed_count": 1,
                "matched_count": 1,
                "notified_count": 1,
                "non_target_count": 0,
                "failure_kind": None,
                "failure_message": None,
                "failure_message_id": None,
                "should_retry": False,
                "should_alert": False,
                "auth_status": "READY",
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 1,
                "run_id": "r2",
                "started_at": "2026-04-04 00:01:00",
                "ended_at": "2026-04-04 00:01:01",
                "checkpoint_before": "b",
                "checkpoint_after": "b",
                "processed_count": 1,
                "matched_count": 1,
                "notified_count": 0,
                "non_target_count": 0,
                "failure_kind": "delivery_failed",
                "failure_message": "fail",
                "failure_message_id": "mid-1",
                "should_retry": True,
                "should_alert": True,
                "auth_status": "READY",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(state_file=state_file, runs_file=runs_file)
    summary = store.load_last_run_summary()
    incident = store.load_incident_state()

    assert summary is not None
    assert summary["last_run_status"] == "error"
    assert summary["last_failure_kind"] == "delivery_failed"
    assert summary["last_success_at"] == "2026-04-04 00:00:01"
    assert incident is not None
    assert incident["kind"] == "delivery_failed"
    assert incident["suppressed_count"] == 2


def test_advance_checkpoint_updates_state_snapshot(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old"}), encoding="utf-8")
    store = JsonlCheckpointStore(state_file=state_file)

    store.advance_checkpoint(Checkpoint(message_id="new"), "run-1")

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_message_id"] == "new"


def test_advance_checkpoint_snapshot_write_failure_is_best_effort(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old"}), encoding="utf-8")
    store = JsonlCheckpointStore(state_file=state_file)

    monkeypatch.setattr(
        "amazon_notify.checkpoint_store.save_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    store.advance_checkpoint(Checkpoint(message_id="new"), "run-1")
    events = _read_jsonl(store.events_file)
    assert any(event.get("checkpoint") == "new" for event in events)


def test_advance_checkpoint_raises_when_event_write_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old"}), encoding="utf-8")
    store = JsonlCheckpointStore(state_file=state_file)

    monkeypatch.setattr(
        store,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("events disk full")),
    )

    with pytest.raises(CheckpointError) as exc_info:
        store.advance_checkpoint(Checkpoint(message_id="new"), "run-1")

    assert exc_info.value.message_id == "new"


def test_advance_checkpoint_error_message_includes_enospc_hint(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old"}), encoding="utf-8")
    store = JsonlCheckpointStore(state_file=state_file)

    monkeypatch.setattr(
        store,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")),
    )

    with pytest.raises(CheckpointError) as exc_info:
        store.advance_checkpoint(Checkpoint(message_id="new"), "run-1")

    assert "ENOSPC" in str(exc_info.value)


def test_append_run_result_raises_checkpoint_error_when_storage_write_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "x"}), encoding="utf-8")
    runs_file = tmp_path / "runs.jsonl"
    store = JsonlCheckpointStore(state_file=state_file, runs_file=runs_file)

    result = RunResult(
        run_id="run-1",
        started_at="2026-04-04 00:00:00",
        ended_at="2026-04-04 00:00:01",
        checkpoint_before="a",
        checkpoint_after="b",
        processed_count=1,
        matched_count=1,
        notified_count=1,
        non_target_count=0,
        failure_kind=None,
        failure_message=None,
        failure_message_id=None,
        should_retry=False,
        should_alert=False,
        auth_status=None,
    )

    monkeypatch.setattr(
        store,
        "_append_jsonl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")),
    )

    with pytest.raises(CheckpointError) as exc_info:
        store.append_run_result(result)

    assert exc_info.value.message_id == "b"
    assert "run result 保存" in str(exc_info.value)
