import json
import sys
from pathlib import Path

import pytest

from amazon_notify import cli, config
from amazon_notify.domain import AuthStatus


@pytest.fixture(autouse=True)
def restore_runtime_paths() -> None:
    original_config_path = config.CONFIG_PATH
    original_credentials_path = config.CREDENTIALS_PATH
    original_token_path = config.TOKEN_PATH
    original_default_log_path = config.DEFAULT_LOG_PATH
    original_runtime_dir = config.RUNTIME_DIR
    yield
    config.CONFIG_PATH = original_config_path
    config.CREDENTIALS_PATH = original_credentials_path
    config.TOKEN_PATH = original_token_path
    config.DEFAULT_LOG_PATH = original_default_log_path
    config.RUNTIME_DIR = original_runtime_dir


def test_validate_config_detects_invalid_values() -> None:
    errors = cli.validate_config(
        {
            "discord_webhook_url": "",
            "max_messages": 0,
            "poll_interval_seconds": "abc",
            "amazon_from_pattern": "[",
            "amazon_subject_pattern": "(",
            "state_file": "",
            "log_file": "",
            "gmail_api_max_retries": 0,
            "discord_base_delay_seconds": 0,
            "pubsub_subscription": "",
        }
    )

    assert any("discord_webhook_url" in err for err in errors)
    assert any("max_messages" in err for err in errors)
    assert any("poll_interval_seconds" in err for err in errors)
    assert any("amazon_from_pattern" in err for err in errors)
    assert any("amazon_subject_pattern" in err for err in errors)
    assert any("state_file" in err for err in errors)
    assert any("log_file" in err for err in errors)
    assert any("gmail_api_max_retries" in err for err in errors)
    assert any("discord_base_delay_seconds" in err for err in errors)
    assert any("pubsub_subscription" in err for err in errors)


def test_validate_config_rejects_too_short_poll_interval() -> None:
    errors = cli.validate_config(
        {
            "discord_webhook_url": "https://discord.invalid/webhook",
            "poll_interval_seconds": 5,
        }
    )
    assert any("短すぎます" in err for err in errors)


def test_main_validate_config_exits_nonzero_for_invalid_config(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_subject_pattern": "(",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--validate-config"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "amazon_subject_pattern" in capsys.readouterr().err


def test_main_validate_config_success(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_from_pattern": r"amazon\.co\.jp",
                "amazon_subject_pattern": "(配達済み|発送)",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--validate-config"])
    cli.main()
    assert "[OK] config.json の検証に成功しました。" in capsys.readouterr().out


def test_main_test_discord_sends_and_exits(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_from_pattern": r"amazon\.co\.jp",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "send_discord_test", lambda webhook, message: calls.append((webhook, message)) or True)
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--test-discord"])

    cli.main()

    stdout = capsys.readouterr().out
    assert "[OK] Discord テスト通知を送信しました。" in stdout
    assert len(calls) == 1
    assert calls[0][0] == "https://discord.invalid/webhook"


def test_main_test_discord_failure_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_from_pattern": r"amazon\.co\.jp",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "send_discord_test", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--test-discord"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_health_check_outputs_json_and_nonzero_when_files_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_from_pattern": r"amazon\.co\.jp",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--health-check"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "degraded"
    check_names = [item["name"] for item in report["checks"]]
    assert "credentials_file_exists" in check_names
    assert "token_file_exists" in check_names


def test_main_health_check_outputs_json_when_config_is_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    missing_path = tmp_path / "missing-config.json"
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(missing_path), "--health-check"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "degraded"
    config_check = next(item for item in report["checks"] if item["name"] == "config_file_exists")
    assert config_check["ok"] is False


def test_main_health_check_includes_runtime_status_summary(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
                "state_file": "state.json",
                "events_file": "events.jsonl",
                "runs_file": "runs.jsonl",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "token.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "last_message_id": "x",
                "active_incident_kind": "delivery_failed",
                "active_incident_at": "2026-04-04 09:00:00",
                "incident_suppressed_count": 3,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "runs.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "started_at": "2026-04-04 08:00:00",
                "ended_at": "2026-04-04 08:00:10",
                "checkpoint_before": "a",
                "checkpoint_after": "b",
                "processed_count": 1,
                "matched_count": 1,
                "notified_count": 0,
                "non_target_count": 0,
                "failure_kind": "delivery_failed",
                "failure_message": "failed",
                "failure_message_id": "mid-1",
                "should_retry": True,
                "should_alert": True,
                "auth_status": "READY",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--health-check"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0

    report = json.loads(capsys.readouterr().out)
    runtime_status = report["runtime_status"]
    assert runtime_status["last_run_status"] == "error"
    assert runtime_status["last_failure_kind"] == "delivery_failed"
    assert runtime_status["active_incident"]["kind"] == "delivery_failed"


def test_main_health_check_reports_corrupted_runtime_records(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
                "state_file": "state.json",
                "events_file": "events.jsonl",
                "runs_file": "runs.jsonl",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "token.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps({"last_message_id": "x"}), encoding="utf-8")
    (tmp_path / "runs.jsonl").write_text(
        '{"schema_version":1,"run_id":"ok"}\n'
        '{"broken":\n'
        '{"schema_version":1,"run_id":"ok-2"}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--health-check"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1

    report = json.loads(capsys.readouterr().out)
    runtime_check = next(item for item in report["checks"] if item["name"] == "runtime_records_valid")
    assert runtime_check["ok"] is False


def test_load_config_or_exit_exits_for_missing_json_and_oserror(monkeypatch, tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(config, "CONFIG_PATH", missing)
    with pytest.raises(SystemExit) as missing_exc:
        cli.load_config_or_exit()
    assert missing_exc.value.code == 1

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", invalid)
    with pytest.raises(SystemExit) as invalid_exc:
        cli.load_config_or_exit()
    assert invalid_exc.value.code == 1

    valid = tmp_path / "valid.json"
    valid.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", valid)
    monkeypatch.setattr(config, "load_config", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(SystemExit) as os_exc:
        cli.load_config_or_exit()
    assert os_exc.value.code == 1


def test_load_config_for_health_check_handles_oserror(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config, "load_config", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")))

    loaded, errors = cli.load_config_for_health_check()
    assert loaded is None
    assert errors


def test_main_reauth_paths(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--reauth"])
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(cli, "run_oauth_flow", lambda *_args, **_kwargs: object())
    cli.main()

    monkeypatch.setattr(cli, "run_oauth_flow", lambda *_args, **_kwargs: None)
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_exits_when_interval_is_not_positive(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--interval", "-1", "--once"])
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_once_exits_nonzero_when_first_run_once_raises(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cli, "run_once", lambda _runtime: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    alerts: list[str] = []
    monkeypatch.setattr(cli, "send_discord_alert", lambda _webhook, message: alerts.append(message) or True)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--once"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert alerts


def test_main_loop_handles_unhandled_exception_and_alerts(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 10,
            }
        ),
        encoding="utf-8",
    )

    calls = {"count": 0}

    def fake_run_once(_runtime) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    monkeypatch.setattr(cli.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(cli, "send_discord_alert", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path)])

    with pytest.raises(KeyboardInterrupt):
        cli.main()


def test_main_loop_continues_after_guard_returns_false(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 10,
            }
        ),
        encoding="utf-8",
    )

    calls = {"count": 0}

    def fake_run_once_with_guard(_runtime: dict) -> bool:
        calls["count"] += 1
        if calls["count"] == 1:
            return True
        if calls["count"] == 2:
            return False
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "run_once_with_guard", fake_run_once_with_guard)
    monkeypatch.setattr(cli.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path)])

    with pytest.raises(KeyboardInterrupt):
        cli.main()

    assert calls["count"] == 3


def test_main_exits_when_config_invalid_in_normal_mode(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_subject_pattern": "(",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--once"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_build_runtime_uses_consistent_default_amazon_pattern() -> None:
    runtime = cli.build_runtime({"discord_webhook_url": "https://discord.invalid/webhook"})
    assert runtime["amazon_pattern"].pattern == r"amazon\.co\.jp"
    assert runtime["events_file"].name == "events.jsonl"
    assert runtime["runs_file"].name == "runs.jsonl"


def test_main_streaming_pull_requires_subscription(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--streaming-pull"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_streaming_pull_runs_with_subscription(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
                "pubsub_subscription": "projects/p/subscriptions/s",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "run_once_with_guard", lambda _runtime: True)
    calls: list[dict] = []
    monkeypatch.setattr(
        cli,
        "run_streaming_pull",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--streaming-pull"])

    cli.main()
    assert len(calls) == 1
    assert calls[0]["subscription_path"] == "projects/p/subscriptions/s"
    assert "heartbeat_file" in calls[0]
    assert "heartbeat_interval_seconds" in calls[0]


def test_main_streaming_pull_reconnects_in_process_before_giving_up(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
                "pubsub_subscription": "projects/p/subscriptions/s",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "run_once_with_guard", lambda _runtime: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _sec: None)

    calls = {"count": 0}

    def fake_run_streaming_pull(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient stream failure")
        return None

    monkeypatch.setattr(cli, "run_streaming_pull", fake_run_streaming_pull)
    monkeypatch.setattr(sys, "argv", ["amazon-notify", "--config", str(config_path), "--streaming-pull"])

    cli.main()
    assert calls["count"] == 2


def test_main_setup_watch_registers_topic(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.app_config, "load_state", lambda *_args, **_kwargs: {"last_message_id": None})
    monkeypatch.setattr(
        cli,
        "get_gmail_service_with_status",
        lambda **_kwargs: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        cli,
        "start_gmail_watch_with_retry",
        lambda *_args, **_kwargs: {"historyId": "123"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--setup-watch",
            "--pubsub-topic",
            "projects/p/topics/t",
        ],
    )

    cli.main()
    assert json.loads(capsys.readouterr().out)["historyId"] == "123"


def test_main_setup_watch_requires_topic(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["amazon-notify", "--config", str(config_path), "--setup-watch"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_setup_watch_exits_when_gmail_service_unavailable(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.app_config, "load_state", lambda *_args, **_kwargs: {"last_message_id": None})
    monkeypatch.setattr(
        cli,
        "get_gmail_service_with_status",
        lambda **_kwargs: (None, AuthStatus.TOKEN_MISSING),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--setup-watch",
            "--pubsub-topic",
            "projects/p/topics/t",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_setup_watch_exits_when_watch_registration_fails(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.app_config, "load_state", lambda *_args, **_kwargs: {"last_message_id": None})
    monkeypatch.setattr(
        cli,
        "get_gmail_service_with_status",
        lambda **_kwargs: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        cli,
        "start_gmail_watch_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("watch failed")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--setup-watch",
            "--pubsub-topic",
            "projects/p/topics/t",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_fallback_watchdog_requires_once(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["amazon-notify", "--config", str(config_path), "--fallback-watchdog"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_fallback_watchdog_skips_polling_when_main_healthy(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "evaluate_failover_watchdog", lambda **_kwargs: False)
    run_calls: list[dict] = []
    monkeypatch.setattr(cli, "run_once_with_guard", lambda runtime: run_calls.append(runtime) or True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["amazon-notify", "--config", str(config_path), "--once", "--fallback-watchdog"],
    )

    cli.main()
    assert not run_calls


def test_main_fallback_watchdog_runs_polling_when_main_unhealthy(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "evaluate_failover_watchdog", lambda **_kwargs: True)
    run_calls: list[dict] = []
    monkeypatch.setattr(cli, "run_once_with_guard", lambda runtime: run_calls.append(runtime) or True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["amazon-notify", "--config", str(config_path), "--once", "--fallback-watchdog"],
    )

    cli.main()
    assert len(run_calls) == 1


def test_main_streaming_pull_rejects_conflicting_fallback_watchdog(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
                "pubsub_subscription": "projects/p/subscriptions/s",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--streaming-pull",
            "--fallback-watchdog",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_streaming_pull_rejects_once_and_interval(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
                "pubsub_subscription": "projects/p/subscriptions/s",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        sys,
        "argv",
        ["amazon-notify", "--config", str(config_path), "--streaming-pull", "--once"],
    )
    with pytest.raises(SystemExit) as once_exc:
        cli.main()
    assert once_exc.value.code == 1

    monkeypatch.setattr(
        sys,
        "argv",
        ["amazon-notify", "--config", str(config_path), "--streaming-pull", "--interval", "30"],
    )
    with pytest.raises(SystemExit) as interval_exc:
        cli.main()
    assert interval_exc.value.code == 1


def test_main_exits_for_invalid_heartbeat_arguments(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "setup_logging", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--once",
            "--heartbeat-interval-seconds",
            "0",
        ],
    )
    with pytest.raises(SystemExit) as interval_exc:
        cli.main()
    assert interval_exc.value.code == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--once",
            "--heartbeat-max-age-seconds",
            "0",
        ],
    )
    with pytest.raises(SystemExit) as max_age_exc:
        cli.main()
    assert max_age_exc.value.code == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--once",
            "--main-service-name",
            "   ",
        ],
    )
    with pytest.raises(SystemExit) as service_exc:
        cli.main()
    assert service_exc.value.code == 1
