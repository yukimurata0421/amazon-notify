import json
from pathlib import Path

from amazon_notify import config, gmail_client, notifier


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_is_transient_network_error_for_timeout_and_hostname_mismatch() -> None:
    assert gmail_client.is_transient_network_error(TimeoutError("timed out"))
    ssl_exc = Exception(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "Hostname mismatch, certificate is not valid for 'gmail.googleapis.com'."
    )
    assert gmail_client.is_transient_network_error(ssl_exc)


def test_is_transient_network_error_respects_max_depth() -> None:
    root = RuntimeError("root")
    current = root
    for idx in range(12):
        next_exc = RuntimeError(f"layer-{idx}")
        current.__cause__ = next_exc
        current = next_exc
    current.__cause__ = RuntimeError("timed out")

    assert not gmail_client.is_transient_network_error(root, max_depth=5)
    assert gmail_client.is_transient_network_error(root, max_depth=20)


def test_mark_issue_and_notify_recovery(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = {"last_message_id": "msg-1"}

    gmail_client.mark_transient_network_issue(state, state_file, TimeoutError("timed out"))
    saved = _read_json(state_file)
    assert saved["transient_network_issue_active"] is True
    assert "timed out" in saved["last_transient_error"]

    calls: list[tuple[str, str]] = []

    def fake_send_recovery(webhook_url: str, message: str) -> None:
        calls.append((webhook_url, message))
        return True

    monkeypatch.setattr(gmail_client, "send_discord_recovery", fake_send_recovery)

    gmail_client.notify_recovery_if_needed("https://discord.invalid/webhook", state, state_file)

    assert len(calls) == 1
    assert "復旧" in calls[0][1]
    assert "timed out" in calls[0][1]

    saved_after = _read_json(state_file)
    assert saved_after["transient_network_issue_active"] is False
    assert "last_transient_error" not in saved_after


def test_notify_recovery_keeps_state_when_discord_send_fails(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = {
        "last_message_id": "msg-1",
        "transient_network_issue_active": True,
        "last_transient_error": "timed out",
        "last_transient_error_at": "2026-04-02 10:00:00",
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(gmail_client, "send_discord_recovery", lambda *_args, **_kwargs: False)

    gmail_client.notify_recovery_if_needed(
        "https://discord.invalid/webhook",
        state,
        state_file,
    )

    saved = _read_json(state_file)
    assert saved["transient_network_issue_active"] is True


def test_save_state_creates_parent_directories(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "runtime" / "state.json"

    config.save_state(state_file, {"last_message_id": "msg-1"})

    assert _read_json(state_file)["last_message_id"] == "msg-1"


def test_run_once_sends_amazon_notification_and_updates_state(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": state_file,
        "max_messages": 10,
        "subject_pattern": None,
    }

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(
        notifier,
        "list_recent_messages",
        lambda service, query, max_results: [{"id": "new-id"}, {"id": "old-id"}],
    )

    def fake_message_detail(service, message_id: str) -> dict:
        return {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {
                        "name": "From",
                        "value": "Amazon.co.jp <order-update@amazon.co.jp>",
                    },
                ]
            },
            "snippet": "配達済みのお知らせ",
        }

    monkeypatch.setattr(notifier, "get_message_detail", fake_message_detail)

    sent: list[dict] = []

    def fake_send_notification(**kwargs) -> bool:
        sent.append(kwargs)
        return True

    monkeypatch.setattr(notifier, "send_discord_notification", fake_send_notification)

    notifier.run_once(runtime)

    assert len(sent) == 1
    assert sent[0]["from_addr"] == "order-update@amazon.co.jp"
    assert sent[0]["url"].endswith("/new-id")

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "new-id"


def test_run_once_does_not_advance_state_when_notification_fails(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": state_file,
        "max_messages": 10,
        "subject_pattern": None,
    }

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(
        notifier,
        "list_recent_messages",
        lambda service, query, max_results: [{"id": "new-id"}, {"id": "old-id"}],
    )

    def fake_message_detail(service, message_id: str) -> dict:
        return {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {
                        "name": "From",
                        "value": "Amazon.co.jp <order-update@amazon.co.jp>",
                    },
                ]
            },
            "snippet": "配達済みのお知らせ",
        }

    monkeypatch.setattr(notifier, "get_message_detail", fake_message_detail)
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **kwargs: False)

    alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda webhook_url, message: alerts.append(message) or True,
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert alerts


def test_run_once_dry_run_does_not_send_notification_or_update_state(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": state_file,
        "max_messages": 10,
        "subject_pattern": None,
        "dry_run": True,
    }

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(
        notifier,
        "list_recent_messages",
        lambda service, query, max_results: [{"id": "new-id"}, {"id": "old-id"}],
    )

    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda service, message_id: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {"name": "From", "value": "Amazon.co.jp <order-update@amazon.co.jp>"},
                ]
            },
            "snippet": "配達済みのお知らせ",
        },
    )

    sent: list[dict] = []
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **kwargs: sent.append(kwargs) or True)

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert not sent


def test_run_once_advances_state_for_non_amazon_mail_and_logs_count(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": state_file,
        "max_messages": 10,
        "subject_pattern": None,
    }

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(
        notifier,
        "list_recent_messages",
        lambda service, query, max_results: [{"id": "new-id"}, {"id": "old-id"}],
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda service, message_id: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "テスト件名"},
                    {"name": "From", "value": "Other Sender <other@example.com>"},
                ]
            },
            "snippet": "non amazon",
        },
    )

    sent: list[dict] = []
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **kwargs: sent.append(kwargs) or True)

    logs: list[str] = []

    def fake_info(message: str, *args) -> None:
        logs.append(message % args if args else message)

    monkeypatch.setattr(notifier.LOGGER, "info", fake_info)

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "new-id"
    assert not sent
    assert any("non_amazon_skipped=1" in message for message in logs)


def test_run_once_marks_transient_issue_when_message_list_times_out(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "id-1"}), encoding="utf-8")

    runtime = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": state_file,
        "max_messages": 10,
        "subject_pattern": None,
    }

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())

    def raise_timeout(service, query, max_results):
        raise TimeoutError("timed out")

    monkeypatch.setattr(notifier, "list_recent_messages", raise_timeout)

    alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda webhook_url, message: alerts.append(message),
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["transient_network_issue_active"] is True
    assert "timed out" in saved["last_transient_error"]
    assert alerts
