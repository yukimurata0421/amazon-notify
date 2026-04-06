import json
from pathlib import Path

from amazon_notify import gmail_client, notifier
from amazon_notify.runtime import RuntimeConfig


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig.from_mapping(
        {
            "discord_webhook_url": "https://discord.invalid/webhook",
            "amazon_from_pattern": r"amazon\.co\.jp",
            "state_file": tmp_path / "state.json",
            "events_file": tmp_path / "events.jsonl",
            "runs_file": tmp_path / "runs.jsonl",
            "max_messages": 20,
            "gmail_api_max_retries": 1,
            "transient_alert_min_duration_seconds": 0.0,
            "transient_alert_cooldown_seconds": 0.0,
        }
    )


def test_e2e_transient_error_then_recovery_notification(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "baseline"}), encoding="utf-8")

    runtime = _runtime(tmp_path)

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
        gmail_client,
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


def test_e2e_transient_alert_threshold_respects_default_window(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "baseline"}), encoding="utf-8")
    state = {"last_message_id": "baseline"}

    timeline = iter([1000.0, 1300.0, 1701.0])
    monkeypatch.setattr(gmail_client.time, "time", lambda: next(timeline))

    alerts: list[str] = []
    monkeypatch.setattr(
        gmail_client,
        "send_discord_alert",
        lambda _webhook_url, message: alerts.append(message) or True,
    )

    for _ in range(3):
        gmail_client.record_transient_issue(
            state,
            state_file,
            TimeoutError("timed out"),
            webhook_url="https://discord.invalid/webhook",
            alert_message="transient issue",
        )

    assert len(alerts) == 1
