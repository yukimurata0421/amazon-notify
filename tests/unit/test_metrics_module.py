from __future__ import annotations

import json
from pathlib import Path

from amazon_notify.commands import metrics as cmd_metrics
from amazon_notify.metrics import (
    _as_int,
    _checkpoint_age_seconds,
    _fmt_num,
    _open_incident_duration_seconds,
    _parse_ts,
    _read_json_object,
    _read_jsonl_rows,
    build_metrics_report,
    format_metrics_plain,
)
from amazon_notify.runtime import RuntimeConfig, RuntimePaths


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


def test_build_metrics_report_and_plain_output(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "active_incident_kind": "source_failed",
                "active_incident_at": "2026-04-10T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "checkpoint_advanced",
                "at": "2026-04-10T00:00:00Z",
            }
        )
        + "\n"
        + json.dumps(
            {
                "event": "incident_suppressed",
                "at": "2026-04-10T00:01:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"notified_count": 2, "failure_kind": None})
        + "\n"
        + json.dumps({"notified_count": 1, "failure_kind": "source_failed"})
        + "\n",
        encoding="utf-8",
    )

    runtime = _runtime(tmp_path)
    report = build_metrics_report(runtime, window=50)
    assert report["window_size"] == 50
    assert report["runs_in_window"] == 2
    assert report["notifications_sent"] == 3
    assert report["run_success_count"] == 1
    assert report["run_failure_count"] == 1
    assert report["incident_suppressed_count"] == 1
    assert report["dedupe_hit_count"] == 1
    assert report["checkpoint_age_seconds"] is not None
    assert report["open_incident_duration_seconds"] is not None

    plain = format_metrics_plain(report)
    assert "window_size=50" in plain
    assert "run_failure_count=1" in plain


def test_metrics_command_wrappers_delegate(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text("", encoding="utf-8")

    runtime = _runtime(tmp_path)
    report = cmd_metrics.build_metrics_report(runtime, window=0)
    assert report["window_size"] == 1

    text = cmd_metrics.format_metrics_plain(report)
    assert "checked_at=" in text


def test_metrics_internal_helpers_and_json_readers(tmp_path: Path) -> None:
    assert _fmt_num(None) == "-"
    assert _fmt_num(1.0) == "1"
    assert _fmt_num(1.25) == "1.250"
    assert _fmt_num(5) == "5"

    assert _as_int(True) == 1
    assert _as_int(False) == 0
    assert _as_int(9) == 9
    assert _as_int("7") == 7
    assert _as_int("bad") == 0

    assert _parse_ts("2026-04-10T00:00:00Z") is not None
    assert _parse_ts("2026-04-10T00:00:00") is not None
    assert _parse_ts("") is None
    assert _parse_ts("bad-ts") is None

    assert _checkpoint_age_seconds([]) is None
    assert _open_incident_duration_seconds(None) is None
    assert (
        _open_incident_duration_seconds(
            {"active_incident_kind": "", "active_incident_at": "x"}
        )
        is None
    )

    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({"k": 1}), encoding="utf-8")
    assert _read_json_object(obj) == {"k": 1}

    list_root = tmp_path / "list.json"
    list_root.write_text("[]", encoding="utf-8")
    assert _read_json_object(list_root) is None

    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert _read_json_object(bad) is None
    assert _read_json_object(tmp_path / "missing.json") is None

    rows_file = tmp_path / "rows.jsonl"
    rows_file.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    assert len(_read_jsonl_rows(rows_file)) == 2

    tail_bad = tmp_path / "tail_bad.jsonl"
    tail_bad.write_text('{"a":1}\n{', encoding="utf-8")
    assert _read_jsonl_rows(tail_bad) == [{"a": 1}]

    middle_bad = tmp_path / "middle_bad.jsonl"
    middle_bad.write_text('{"a":1}\n{\n{"b":2}\n', encoding="utf-8")
    assert _read_jsonl_rows(middle_bad) == [{"a": 1}]
