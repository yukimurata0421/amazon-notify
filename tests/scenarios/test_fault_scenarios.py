"""Composite failure scenarios: contract + structure under stress (CI-friendly)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from amazon_notify.checkpoint_store import JsonlCheckpointStore
from amazon_notify.domain import Checkpoint
from amazon_notify.errors import CheckpointError
from amazon_notify.runtime import RuntimeConfig, RuntimePaths
from amazon_notify.status import build_doctor_report


def _minimal_runtime(tmp_path: Path) -> RuntimeConfig:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"discord_webhook_url": "https://discord.invalid/webhook"}),
        encoding="utf-8",
    )
    paths = RuntimePaths(
        runtime_dir=tmp_path,
        config=cfg_path,
        credentials=tmp_path / "credentials.json",
        token=tmp_path / "token.json",
        default_log=tmp_path / "logs" / "amazon_mail_notifier.log",
    )
    return RuntimeConfig.from_mapping(
        {"discord_webhook_url": "https://discord.invalid/webhook"},
        paths=paths,
    )


def test_events_jsonl_middle_corruption_is_not_ignored(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"r1",'
        '"at":"2026-01-01T00:00:00+00:00","checkpoint":"a","source":"x"}\n'
        "{broken middle line}\n"
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"r2",'
        '"at":"2026-01-02T00:00:00+00:00","checkpoint":"b","source":"x"}\n',
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)
    with pytest.raises(CheckpointError, match="途中行が破損"):
        store.load_checkpoint()


def test_truncated_tail_line_is_ignored_for_checkpoint_load(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"r1",'
        '"at":"2026-01-01T00:00:00+00:00","checkpoint":"good","source":"x"}\n'
        '{"broken":',
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)
    assert store.load_checkpoint().message_id == "good"


def test_rebuild_indexes_restores_summaries_after_manual_index_removal(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "x"}), encoding="utf-8")
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r1",
                "at": "2026-01-01T00:00:00+00:00",
                "checkpoint": "cp1",
                "source": "pipeline_commit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
                "checkpoint_before": None,
                "checkpoint_after": "cp1",
                "processed_count": 0,
                "matched_count": 0,
                "notified_count": 0,
                "non_target_count": 0,
                "failure_kind": None,
                "failure_message": None,
                "failure_message_id": None,
                "should_retry": False,
                "should_alert": False,
                "auth_status": "READY",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    store = JsonlCheckpointStore(
        state_file=state_file, events_file=events_file, runs_file=runs_file
    )
    store.rebuild_indexes()
    assert store.events_checkpoint_index_file.exists()
    assert store.runs_summary_index_file.exists()

    store.events_checkpoint_index_file.unlink()
    store.runs_summary_index_file.unlink()

    rebuilt = store.rebuild_indexes()
    assert rebuilt["checkpoint_index"] is True
    assert rebuilt["run_summary_index"] is True
    assert store.load_checkpoint().message_id == "cp1"
    assert store.load_last_run_summary() is not None


def test_doctor_flags_stale_incident_state_versus_event_log(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_message_id": "m1",
                "active_incident_kind": "delivery_failed",
                "active_incident_at": "2026-04-01T00:00:00+00:00",
                "incident_suppressed_count": 0,
            }
        ),
        encoding="utf-8",
    )
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "incident_recovered",
                "run_id": "r1",
                "at": "2026-04-01T01:00:00+00:00",
                "kind": "delivery_failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runs_path = tmp_path / "runs.jsonl"
    runs_path.write_text("", encoding="utf-8")

    runtime = _minimal_runtime(tmp_path)

    exit_code, report = build_doctor_report(runtime)
    assert exit_code == 1
    assert report["status"] == "degraded"
    inc_check = next(
        c for c in report["checks"] if c["name"] == "incident_lifecycle_consistent"
    )
    assert inc_check["ok"] is False


def test_advance_checkpoint_surfaces_enospc_as_checkpoint_error(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    events_file = tmp_path / "events.jsonl"

    store = JsonlCheckpointStore(state_file=state_file, events_file=events_file)

    with patch.object(
        JsonlCheckpointStore,
        "_append_jsonl",
        side_effect=OSError(28, "No space left on device"),
    ):
        with pytest.raises(CheckpointError, match=r"ENOSPC|ディスク"):
            store.advance_checkpoint(Checkpoint(message_id="mid"), "r1")
