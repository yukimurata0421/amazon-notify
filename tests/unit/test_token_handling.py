import json
from pathlib import Path

import amazon_mail_notifier as notifier
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
    monkeypatch.setattr(notifier, "GOOGLE_IMPORT_ERROR", ModuleNotFoundError("missing google libs"))
    with pytest.raises(ModuleNotFoundError, match="pip install -r requirements.txt"):
        notifier.ensure_google_dependencies()


def test_get_gmail_service_missing_token_alerts_once(monkeypatch, tmp_path: Path) -> None:
    missing_token_path = tmp_path / "missing-token.json"
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "x"}), encoding="utf-8")
    state = notifier.load_state(state_file)

    monkeypatch.setattr(notifier, "TOKEN_PATH", missing_token_path)

    alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda webhook_url, message: alerts.append(message),
    )

    first = notifier.get_gmail_service(
        webhook_url="https://discord.invalid/webhook",
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
    )
    assert first is None
    assert len(alerts) == 1

    state_after_first = notifier.load_state(state_file)
    second = notifier.get_gmail_service(
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
    monkeypatch.setattr(notifier, "TOKEN_PATH", missing_token_path)

    monkeypatch.setattr(notifier, "run_oauth_flow", lambda: DummyCreds(valid=True))
    monkeypatch.setattr(notifier, "build", lambda *args, **kwargs: object())

    service = notifier.get_gmail_service(allow_oauth_interactive=True)
    assert service is not None


def test_get_gmail_service_token_recovery_notifies_once(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(notifier, "TOKEN_PATH", token_path)

    state_file = tmp_path / "state.json"
    state = {
        "last_message_id": "x",
        "token_issue_active": True,
        "token_issue_reason": "token.json が見つかりません",
        "token_issue_at": "2026-04-01 21:00:00",
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(notifier.Credentials, "from_authorized_user_file", lambda *args, **kwargs: DummyCreds(valid=True))
    monkeypatch.setattr(notifier, "build", lambda *args, **kwargs: object())

    recoveries: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_recovery",
        lambda webhook_url, message: recoveries.append(message) or True,
    )

    service = notifier.get_gmail_service(
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
    monkeypatch.setattr(notifier, "TOKEN_PATH", token_path)

    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(
        notifier.Credentials,
        "from_authorized_user_file",
        lambda *args, **kwargs: DummyCreds(valid=False, expired=True, refresh_token="r"),
    )
    monkeypatch.setattr(
        notifier,
        "refresh_with_retry",
        lambda creds, retries=3, base_delay=2: RuntimeError("refresh failed"),
    )

    oauth_calls: list[str] = []
    monkeypatch.setattr(notifier, "run_oauth_flow", lambda: oauth_calls.append("called"))

    alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda webhook_url, message: alerts.append(message) or True,
    )

    service = notifier.get_gmail_service(
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
