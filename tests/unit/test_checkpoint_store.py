import json
from pathlib import Path

from amazon_notify.checkpoint_store import JsonlCheckpointStore
from amazon_notify.domain import Checkpoint, RunResult


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
