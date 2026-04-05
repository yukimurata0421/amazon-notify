import json
from pathlib import Path

from amazon_notify import gmail_client, notifier


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_e2e_transient_error_then_recovery_notification(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "baseline"}), encoding="utf-8")

    runtime = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": state_file,
        "max_messages": 20,
        "gmail_api_max_retries": 1,
        "subject_pattern": None,
    }

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )

    sequence = [TimeoutError("timed out"), []]

    def fake_list_recent_messages(service, query, max_results):
        next_item = sequence.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    monkeypatch.setattr(notifier, "list_recent_messages", fake_list_recent_messages)

    alerts: list[str] = []
    recoveries: list[str] = []

    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda webhook_url, message: alerts.append(message) or True,
    )
    monkeypatch.setattr(
        gmail_client,
        "send_discord_recovery",
        lambda webhook_url, message: recoveries.append(message) or True,
    )

    notifier.run_once(runtime)
    after_failure = _read_json(state_file)
    assert after_failure["transient_network_issue_active"] is True
    assert len(alerts) == 1

    notifier.run_once(runtime)
    after_recovery = _read_json(state_file)
    assert after_recovery["transient_network_issue_active"] is False
    assert len(recoveries) == 1
    assert "復旧" in recoveries[0]
