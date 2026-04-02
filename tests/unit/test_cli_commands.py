import json
import sys
from pathlib import Path

import pytest

from amazon_notify import cli, config


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
        }
    )

    assert any("discord_webhook_url" in err for err in errors)
    assert any("max_messages" in err for err in errors)
    assert any("poll_interval_seconds" in err for err in errors)
    assert any("amazon_from_pattern" in err for err in errors)
    assert any("amazon_subject_pattern" in err for err in errors)
    assert any("state_file" in err for err in errors)
    assert any("log_file" in err for err in errors)


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

    monkeypatch.setattr(cli, "run_oauth_flow", lambda: object())
    cli.main()

    monkeypatch.setattr(cli, "run_oauth_flow", lambda: None)
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


def test_main_loop_handles_unhandled_exception_and_alerts(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "max_messages": 10,
                "poll_interval_seconds": 1,
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
