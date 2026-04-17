from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from amazon_notify.commands import arguments, dispatch
from tests.unit.notifier_test_helpers import build_runtime


def test_build_parser_parses_known_options() -> None:
    parser = arguments.build_parser()
    parsed = parser.parse_args(
        [
            "--config",
            "cfg.json",
            "--once",
            "--interval",
            "30",
            "--metrics",
            "--metrics-plain",
            "--metrics-window",
            "12",
            "--streaming-pull",
            "--pubsub-subscription",
            "projects/p/subscriptions/s",
            "--pubsub-trigger-queue-size",
            "111",
            "--watch-label-ids",
            "INBOX,IMPORTANT",
            "--scenario-harness",
            "--scenario-names",
            "truncated_jsonl",
        ]
    )

    assert parsed.config == "cfg.json"
    assert parsed.once is True
    assert parsed.interval == 30
    assert parsed.metrics is True
    assert parsed.metrics_plain is True
    assert parsed.metrics_window == 12
    assert parsed.streaming_pull is True
    assert parsed.pubsub_subscription == "projects/p/subscriptions/s"
    assert parsed.pubsub_pending_warn_threshold == 111
    assert parsed.watch_label_ids == "INBOX,IMPORTANT"
    assert parsed.scenario_harness is True
    assert parsed.scenario_names == "truncated_jsonl"


def test_validate_action_conflicts_allows_single_action() -> None:
    args = argparse.Namespace(
        reauth=False,
        health_check=False,
        validate_config=False,
        test_discord=False,
        setup_watch=False,
        rebuild_indexes=False,
        status=True,
        doctor=False,
        verify_state=False,
        metrics=False,
        streaming_pull=False,
        scenario_harness=False,
    )
    arguments.validate_action_conflicts(args)


def test_validate_action_conflicts_rejects_multiple_actions(monkeypatch) -> None:
    args = argparse.Namespace(
        reauth=False,
        health_check=False,
        validate_config=False,
        test_discord=False,
        setup_watch=False,
        rebuild_indexes=False,
        status=True,
        doctor=True,
        verify_state=False,
        metrics=False,
        streaming_pull=False,
        scenario_harness=False,
    )
    errors: list[str] = []
    monkeypatch.setattr(arguments, "stderr_error", lambda msg: errors.append(msg))

    with pytest.raises(SystemExit) as exc_info:
        arguments.validate_action_conflicts(args)
    assert exc_info.value.code == 1
    assert errors
    assert "--status" in errors[0]
    assert "--doctor" in errors[0]


def test_handle_rebuild_indexes_false_when_not_requested(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(rebuild_indexes=False)
    assert dispatch.handle_rebuild_indexes(args, runtime) is False


def test_handle_rebuild_indexes_outputs_json(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime = build_runtime(tmp_path)

    class _FakeStore:
        def __init__(self, *, state_file: Path, events_file: Path, runs_file: Path):
            assert state_file == runtime.state_file
            assert events_file == runtime.events_file
            assert runs_file == runtime.runs_file

        def rebuild_indexes(self) -> dict[str, bool]:
            return {"checkpoint_index": True, "run_summary_index": False}

    monkeypatch.setattr(dispatch, "JsonlCheckpointStore", _FakeStore)
    args = argparse.Namespace(rebuild_indexes=True)
    assert dispatch.handle_rebuild_indexes(args, runtime) is True

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["checkpoint_index_rebuilt"] is True
    assert payload["run_summary_index_rebuilt"] is False


def test_handle_status_report_raises_system_exit(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(status=True)
    monkeypatch.setattr(
        dispatch.status_command, "build_status_report", lambda _runtime: (3, {"x": 1})
    )
    monkeypatch.setattr(
        dispatch.status_command,
        "format_status_summary",
        lambda report: f"summary:{report['x']}",
    )

    with pytest.raises(SystemExit) as exc_info:
        dispatch.handle_status_report(args, runtime)
    assert exc_info.value.code == 3
    assert "summary:1" in capsys.readouterr().out


def test_handle_status_report_returns_false_when_disabled(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(status=False)
    assert dispatch.handle_status_report(args, runtime) is False


def test_handle_doctor_report_raises_system_exit(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(doctor=True)
    monkeypatch.setattr(
        dispatch.status_command,
        "build_doctor_report",
        lambda _runtime: (2, {"status": "degraded"}),
    )

    with pytest.raises(SystemExit) as exc_info:
        dispatch.handle_doctor_report(args, runtime)
    assert exc_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_handle_doctor_report_returns_false_when_disabled(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(doctor=False)
    assert dispatch.handle_doctor_report(args, runtime) is False


def test_handle_verify_state_report_raises_system_exit(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(verify_state=True)
    monkeypatch.setattr(
        dispatch.status_command,
        "build_doctor_report",
        lambda _runtime: (1, {"status": "error"}),
    )

    with pytest.raises(SystemExit) as exc_info:
        dispatch.handle_verify_state_report(args, runtime)
    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_handle_verify_state_report_returns_false_when_disabled(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(verify_state=False)
    assert dispatch.handle_verify_state_report(args, runtime) is False


def test_handle_metrics_report_json_and_plain(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(
        dispatch.status_command,
        "build_metrics_report",
        lambda _runtime, recent_run_window: {"window": recent_run_window, "ok": True},
    )
    monkeypatch.setattr(
        dispatch.status_command,
        "format_metrics_plain",
        lambda report: f"plain:{report['window']}",
    )

    args_json = argparse.Namespace(metrics=True, metrics_plain=False, metrics_window=9)
    with pytest.raises(SystemExit) as exc_info_json:
        dispatch.handle_metrics_report(args_json, runtime)
    assert exc_info_json.value.code == 0
    assert json.loads(capsys.readouterr().out)["window"] == 9

    args_plain = argparse.Namespace(metrics=True, metrics_plain=True, metrics_window=7)
    with pytest.raises(SystemExit) as exc_info_plain:
        dispatch.handle_metrics_report(args_plain, runtime)
    assert exc_info_plain.value.code == 0
    assert "plain:7" in capsys.readouterr().out


def test_handle_metrics_report_returns_false_when_disabled(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    args = argparse.Namespace(metrics=False, metrics_plain=False, metrics_window=5)
    assert dispatch.handle_metrics_report(args, runtime) is False
