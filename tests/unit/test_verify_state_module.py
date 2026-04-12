from __future__ import annotations

import json
from pathlib import Path

from amazon_notify.runtime import RuntimeConfig, RuntimePaths
from amazon_notify.verify_state import (
    _check_checkpoint_timestamp_monotonic,
    _check_incident_event_lifecycle,
    _parse_ts,
    _read_jsonl_rows,
    run_verify_state,
)


def _runtime(tmp_path: Path) -> RuntimeConfig:
    cfg = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "state_file": "state.json",
        "events_file": "events.jsonl",
        "runs_file": "runs.jsonl",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    paths = RuntimePaths(
        runtime_dir=tmp_path,
        config=config_path,
        credentials=tmp_path / "credentials.json",
        token=tmp_path / "token.json",
        default_log=tmp_path / "logs" / "amazon_mail_notifier.log",
    )
    return RuntimeConfig.from_mapping(cfg, paths=paths)


def test_run_verify_state_ok_with_consistent_events(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps({"last_message_id": "cp-2"}), encoding="utf-8"
    )
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r1",
                "at": "2026-04-10T00:00:00+00:00",
                "checkpoint": "cp-1",
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r2",
                "at": "2026-04-10T00:01:00+00:00",
                "checkpoint": "cp-2",
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 1,
                "event": "incident_opened",
                "run_id": "r3",
                "at": "2026-04-10T00:02:00+00:00",
                "kind": "source_failed",
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 1,
                "event": "incident_suppressed",
                "run_id": "r4",
                "at": "2026-04-10T00:03:00+00:00",
                "kind": "source_failed",
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 1,
                "event": "incident_recovered",
                "run_id": "r5",
                "at": "2026-04-10T00:04:00+00:00",
                "kind": "source_failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    code, report = run_verify_state(_runtime(tmp_path))
    assert code == 0
    assert report["status"] == "ok"
    assert report["verify_state"]["extra_check_count"] == 2


def test_checkpoint_timestamp_monotonic_validation_failures(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps({"event": "checkpoint_advanced", "at": "2026-04-10T00:01:00+00:00"})
        + "\n"
        + json.dumps(
            {"event": "checkpoint_advanced", "at": "2026-04-10T00:00:00+00:00"}
        )
        + "\n",
        encoding="utf-8",
    )
    ok, detail = _check_checkpoint_timestamp_monotonic(events)
    assert ok is False
    assert "逆行" in detail

    events.write_text(
        json.dumps({"event": "checkpoint_advanced", "at": "bad-ts"}) + "\n",
        encoding="utf-8",
    )
    ok2, detail2 = _check_checkpoint_timestamp_monotonic(events)
    assert ok2 is True or "解析" in detail2


def test_incident_lifecycle_validation_failures_and_success(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"

    events.write_text(
        json.dumps({"event": "incident_suppressed", "kind": "source_failed"}) + "\n",
        encoding="utf-8",
    )
    ok, detail = _check_incident_event_lifecycle(events)
    assert ok is False
    assert "open 前" in detail

    events.write_text(
        json.dumps({"event": "incident_opened", "kind": "source_failed"})
        + "\n"
        + json.dumps({"event": "incident_suppressed", "kind": "delivery_failed"})
        + "\n",
        encoding="utf-8",
    )
    ok2, detail2 = _check_incident_event_lifecycle(events)
    assert ok2 is False
    assert "不一致" in detail2

    events.write_text("", encoding="utf-8")
    ok3, detail3 = _check_incident_event_lifecycle(events)
    assert ok3 is True
    assert "存在しない" in detail3


def test_read_jsonl_rows_and_parse_ts_paths(tmp_path: Path) -> None:
    missing_rows, missing_err = _read_jsonl_rows(tmp_path / "missing.jsonl")
    assert missing_rows == []
    assert missing_err is None

    tail_bad = tmp_path / "tail_bad.jsonl"
    tail_bad.write_text('{"event":"x"}\n{', encoding="utf-8")
    rows, err = _read_jsonl_rows(tail_bad)
    assert err is None
    assert len(rows) == 1

    middle_bad = tmp_path / "middle_bad.jsonl"
    middle_bad.write_text('{"event":"x"}\n{\n{"event":"y"}\n', encoding="utf-8")
    rows2, err2 = _read_jsonl_rows(middle_bad)
    assert rows2 == []
    assert err2 is not None

    assert _parse_ts("2026-04-10T00:00:00Z") is not None
    assert _parse_ts("2026-04-10T00:00:00+00:00") is not None
    assert _parse_ts("") is None
