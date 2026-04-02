import json
from pathlib import Path

from amazon_notify import config, gmail_client
from amazon_notify import cli
import pytest


class DummyCreds:
    def __init__(self, valid: bool = True, expired: bool = False, refresh_token: str | None = "r"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token

    def to_json(self) -> str:
        return "{}"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_ensure_google_dependencies_gives_install_hint(monkeypatch) -> None:
    monkeypatch.setattr(gmail_client, "GOOGLE_IMPORT_ERROR", ModuleNotFoundError("missing google libs"))
    with pytest.raises(ModuleNotFoundError, match="pip install \\."):
        gmail_client.ensure_google_dependencies()


def test_get_gmail_service_missing_token_alerts_once(monkeypatch, tmp_path: Path) -> None:
    missing_token_path = tmp_path / "missing-token.json"
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "x"}), encoding="utf-8")
    state = config.load_state(state_file)

    monkeypatch.setattr(config, "TOKEN_PATH", missing_token_path)

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda webhook_url, message: alerts.append(message))

    first = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
    )
    assert first is None
    assert len(alerts) == 1

    state_after_first = config.load_state(state_file)
    second = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state_after_first,
        state_file=state_file,
        allow_oauth_interactive=False,
    )
    assert second is None
    assert len(alerts) == 1

    saved = _read_json(state_file)
    assert saved["token_issue_active"] is True
    assert "token.json" in saved["token_issue_reason"]


def test_get_gmail_service_allow_oauth_interactive_uses_run_oauth_flow(monkeypatch, tmp_path: Path) -> None:
    missing_token_path = tmp_path / "missing-token.json"
    monkeypatch.setattr(config, "TOKEN_PATH", missing_token_path)

    monkeypatch.setattr(gmail_client, "run_oauth_flow", lambda: DummyCreds(valid=True))
    monkeypatch.setattr(gmail_client, "build", lambda *args, **kwargs: object())

    service = gmail_client.get_gmail_service(allow_oauth_interactive=True)
    assert service is not None


def test_get_gmail_service_token_recovery_notifies_once(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)

    state_file = tmp_path / "state.json"
    state = {
        "last_message_id": "x",
        "token_issue_active": True,
        "token_issue_reason": "token.json が見つかりません",
        "token_issue_at": "2026-04-01 21:00:00",
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(gmail_client.Credentials, "from_authorized_user_file", lambda *args, **kwargs: DummyCreds(valid=True))
    monkeypatch.setattr(gmail_client, "build", lambda *args, **kwargs: object())

    recoveries: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_recovery", lambda webhook_url, message: recoveries.append(message) or True)

    service = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
    )
    assert service is not None
    assert len(recoveries) == 1

    saved = _read_json(state_file)
    assert saved["token_issue_active"] is False
    assert "token_issue_reason" not in saved


def test_get_gmail_service_refresh_failure_does_not_start_oauth_in_noninteractive_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_PATH", token_path)

    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(
        gmail_client.Credentials,
        "from_authorized_user_file",
        lambda *args, **kwargs: DummyCreds(valid=False, expired=True, refresh_token="r"),
    )
    monkeypatch.setattr(
        gmail_client,
        "refresh_with_retry",
        lambda creds, retries=3, base_delay=2: RuntimeError("refresh failed"),
    )

    oauth_calls: list[str] = []
    monkeypatch.setattr(gmail_client, "run_oauth_flow", lambda: oauth_calls.append("called"))

    alerts: list[str] = []
    monkeypatch.setattr(gmail_client, "send_discord_alert", lambda webhook_url, message: alerts.append(message) or True)

    service = gmail_client.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
    )

    assert service is None
    assert not oauth_calls
    assert len(alerts) == 1

    saved = _read_json(state_file)
    assert saved["token_issue_active"] is True
    assert "自動更新に失敗" in saved["token_issue_reason"]


def test_compile_optional_pattern_exits_for_invalid_regex(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.compile_optional_pattern("[", "amazon_subject_pattern")

    assert exc_info.value.code == 1
    assert "amazon_subject_pattern" in capsys.readouterr().err


def test_configure_runtime_paths_updates_default_locations(tmp_path: Path) -> None:
    original_config_path = config.CONFIG_PATH
    original_credentials_path = config.CREDENTIALS_PATH
    original_token_path = config.TOKEN_PATH
    original_default_log_path = config.DEFAULT_LOG_PATH
    original_runtime_dir = config.RUNTIME_DIR

    config_path = tmp_path / "runtime" / "config.json"
    try:
        runtime_dir = config.configure_runtime_paths(config_path)

        assert runtime_dir == config_path.parent.resolve()
        assert config.CONFIG_PATH == config_path.resolve()
        assert config.CREDENTIALS_PATH == config_path.parent.resolve() / "credentials.json"
        assert config.TOKEN_PATH == config_path.parent.resolve() / "token.json"
        assert config.resolve_runtime_path("state.json") == config_path.parent.resolve() / "state.json"
    finally:
        config.CONFIG_PATH = original_config_path
        config.CREDENTIALS_PATH = original_credentials_path
        config.TOKEN_PATH = original_token_path
        config.DEFAULT_LOG_PATH = original_default_log_path
        config.RUNTIME_DIR = original_runtime_dir
