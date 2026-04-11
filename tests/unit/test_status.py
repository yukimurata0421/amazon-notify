from __future__ import annotations

import json
from pathlib import Path

import pytest

from amazon_notify.runtime import RuntimeConfig, RuntimePaths
from amazon_notify.status import (
    build_doctor_report,
    build_metrics_report,
    build_status_report,
    format_metrics_plain,
    format_status_summary,
)


def _runtime(tmp_path: Path, **cfg_overrides: object) -> RuntimeConfig:
    cfg_path = tmp_path / "config.json"
    base = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "state_file": "state.json",
        "events_file": "events.jsonl",
        "runs_file": "runs.jsonl",
    }
    base.update(cfg_overrides)
    cfg_path.write_text(json.dumps(base), encoding="utf-8")
    paths = RuntimePaths(
        runtime_dir=tmp_path,
        config=cfg_path,
        credentials=tmp_path / "credentials.json",
        token=tmp_path / "token.json",
        default_log=tmp_path / "logs" / "amazon_mail_notifier.log",
    )
    return RuntimeConfig.from_mapping(base, paths=paths)


def test_build_status_and_doctor_ok_when_aligned(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "last_message_id": "cp1",
                "last_run_summary": {
                    "last_run_status": "ok",
                    "last_failure_kind": None,
                    "checkpoint_before": None,
                    "checkpoint_after": "cp1",
                    "auth_status": "READY",
                    "last_success_at": "2026-04-01T00:00:01+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r1",
                "at": "2026-04-01T00:00:00+00:00",
                "checkpoint": "cp1",
                "source": "pipeline_commit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "started_at": "2026-04-01T00:00:00+00:00",
                "ended_at": "2026-04-01T00:00:01+00:00",
                "checkpoint_before": None,
                "checkpoint_after": "cp1",
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
        + "\n",
        encoding="utf-8",
    )

    rt = _runtime(tmp_path)
    code, summary = build_status_report(rt)
    assert code == 0
    assert summary["status"] == "ok"

    code2, doctor = build_doctor_report(rt)
    assert code2 == 0
    assert doctor["status"] == "ok"
    assert all(c["ok"] for c in doctor["checks"])


def test_build_metrics_includes_dedupe_entry_count(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")
    (tmp_path / ".discord_dedupe_state.json").write_text(
        json.dumps({"schema_version": 1, "entries": {"k1": {"last_sent_at": 1.0}}}),
        encoding="utf-8",
    )

    rt = _runtime(tmp_path)
    m = build_metrics_report(rt, recent_run_window=5)
    assert m["dedupe"]["entry_count"] == 1
    assert m["dedupe"]["readable"] is True
    plain = format_metrics_plain(m)
    assert "dedupe_entries: 1" in plain


def test_build_metrics_handles_corrupt_events_jsonl(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        '{"event":"checkpoint_advanced","at":"2026-01-01T00:00:00+00:00"}\n'
        "{bad}\n"
        '{"event":"checkpoint_advanced","at":"2026-01-02T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    rt = _runtime(tmp_path)
    m = build_metrics_report(rt)
    assert m["checkpoint"]["last_advanced_at"] is None
    assert m["incident_events"]["suppressed_total"] == 0


def test_build_metrics_handles_corrupt_runs_jsonl(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text(
        '{"run_id":"a"}\n{bad}\n{"run_id":"b"}\n',
        encoding="utf-8",
    )

    rt = _runtime(tmp_path)
    m = build_metrics_report(rt)
    assert m["runs_recent"]["window_runs"] == 0


def test_build_metrics_open_incident_duration(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "active_incident_kind": "delivery_failed",
                "active_incident_at": "2026-01-01T00:00:00+00:00",
                "incident_suppressed_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    rt = _runtime(tmp_path)
    m = build_metrics_report(rt)
    assert m["incident"]["status"] == "open"
    assert m["incident"]["open_duration_seconds"] is not None


def test_build_metrics_dedupe_non_object_root(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")
    (tmp_path / ".discord_dedupe_state.json").write_text("[]", encoding="utf-8")

    rt = _runtime(tmp_path)
    m = build_metrics_report(rt)
    assert m["dedupe"]["readable"] is False
    assert m["dedupe"]["entry_count"] == 0


def test_build_metrics_counts_incident_suppressed_events(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "incident_suppressed",
                "run_id": "r",
                "at": "2026-01-01T00:00:00+00:00",
                "kind": "x",
                "suppressed_count": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    rt = _runtime(tmp_path)
    m = build_metrics_report(rt)
    assert m["incident_events"]["suppressed_total"] == 1


def test_doctor_with_checkpoint_index_file(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    (tmp_path / "state.json").write_text(
        json.dumps({"last_message_id": "cp1"}), encoding="utf-8"
    )
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r1",
                "at": "2026-04-01T00:00:00+00:00",
                "checkpoint": "cp1",
                "source": "pipeline_commit",
            }
        )
        + "\n"
    )
    events.write_text(payload, encoding="utf-8")
    offset = 0
    eof = events.stat().st_size
    (tmp_path / "events.jsonl.checkpoint.index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checkpoint": "cp1",
                "offset": offset,
                "eof_size": eof,
                "updated_at": "2026-04-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    rt = _runtime(tmp_path)
    code, _doctor = build_doctor_report(rt)
    assert code == 0


def test_doctor_with_runs_summary_index(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "last_message_id": "cp1",
                "last_run_summary": {
                    "last_run_status": "ok",
                    "last_failure_kind": None,
                    "checkpoint_before": None,
                    "checkpoint_after": "cp1",
                    "auth_status": "READY",
                    "last_success_at": "2026-04-01T00:00:01+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "r1",
                "at": "2026-04-01T00:00:00+00:00",
                "checkpoint": "cp1",
                "source": "pipeline_commit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runs = tmp_path / "runs.jsonl"
    line = json.dumps(
        {
            "schema_version": 1,
            "run_id": "run-1",
            "started_at": "2026-04-01T00:00:00+00:00",
            "ended_at": "2026-04-01T00:00:01+00:00",
            "checkpoint_before": None,
            "checkpoint_after": "cp1",
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
    runs.write_text(line + "\n", encoding="utf-8")
    roffset = 0
    reof = runs.stat().st_size
    (tmp_path / "runs.jsonl.summary.index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "offset": roffset,
                "eof_size": reof,
                "summary": {
                    "last_run_status": "ok",
                    "last_failure_kind": None,
                    "checkpoint_before": None,
                    "checkpoint_after": "cp1",
                    "auth_status": "READY",
                    "last_success_at": "2026-04-01T00:00:01+00:00",
                },
                "updated_at": "2026-04-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    rt = _runtime(tmp_path)
    code, _doctor = build_doctor_report(rt)
    assert code == 0


@pytest.mark.parametrize(
    ("incident_status", "kind", "needle"),
    [
        ("suppressed", "k", "suppressed(kind=k"),
        ("suppressed", None, "incident: suppressed\n"),
        ("open", "k", "open(kind=k"),
        ("open", None, "incident: open\n"),
        ("recovered", "k", "recovered(kind=k"),
        ("recovered", None, "incident: recovered\n"),
        ("none", None, "incident: none"),
    ],
)
def test_format_status_summary_incident_branches(
    incident_status: str, kind: str | None, needle: str
) -> None:
    incident: dict = {}
    if kind is not None:
        incident["kind"] = kind
        incident["suppressed_count"] = 1
    text = format_status_summary(
        {
            "status": "ok",
            "frontier": "",
            "last_success_at": None,
            "last_failure_kind": None,
            "incident_status": incident_status,
            "incident": incident,
            "consistency": {
                "checkpoint_consistent": True,
                "run_summary_consistent": False,
                "incident_consistent": True,
            },
        }
    )
    assert needle in text


def test_doctor_reports_events_jsonl_middle_corruption(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"a",'
        '"at":"2026-01-01T00:00:00+00:00","checkpoint":"x","source":"y"}\n'
        "{not json}\n"
        '{"schema_version":1,"event":"checkpoint_advanced","run_id":"b",'
        '"at":"2026-01-02T00:00:00+00:00","checkpoint":"z","source":"y"}\n',
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    rt = _runtime(tmp_path)
    code, doctor = build_doctor_report(rt)
    assert code == 1
    assert doctor["status"] == "degraded"
    ev = next(c for c in doctor["checks"] if c["name"] == "events_jsonl_readable")
    assert ev["ok"] is False


def test_doctor_reports_runs_jsonl_middle_corruption(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text(
        '{"schema_version":1,"run_id":"a"}\n{bad}\n{"schema_version":1,"run_id":"b"}\n',
        encoding="utf-8",
    )

    rt = _runtime(tmp_path)
    code, doctor = build_doctor_report(rt)
    assert code == 1
    ru = next(c for c in doctor["checks"] if c["name"] == "runs_jsonl_readable")
    assert ru["ok"] is False


def test_doctor_reports_unreadable_state_file(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0)
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    rt = _runtime(tmp_path)
    code, doctor = build_doctor_report(rt)
    state.chmod(0o644)
    assert code == 1
    st = next(c for c in doctor["checks"] if c["name"] == "state_json_readable")
    assert st["ok"] is False
