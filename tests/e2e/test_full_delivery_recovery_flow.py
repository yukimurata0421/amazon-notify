from __future__ import annotations

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
            "transient_state_file": tmp_path / "transient_state.json",
            "events_file": tmp_path / "events.jsonl",
            "runs_file": tmp_path / "runs.jsonl",
            "max_messages": 20,
            "gmail_api_max_retries": 1,
            "transient_alert_min_duration_seconds": 0.0,
            "transient_alert_cooldown_seconds": 0.0,
        }
    )


def test_e2e_delivery_then_incident_then_recovery(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.state_file.write_text("{}", encoding="utf-8")
    runtime.transient_state_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )

    # run1: one message, run2: transient failure, run3: empty(recovery)
    page_seq: list[object] = [
        [{"id": "m-1"}],
        TimeoutError("timeout"),
        [{"id": "m-1"}],
    ]

    def fake_list_recent_messages_page(
        _service,
        *,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ):
        assert query == "in:inbox"
        _ = max_results
        if page_token is not None:
            return [], None
        next_item = page_seq.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item, None

    monkeypatch.setattr(
        notifier, "list_recent_messages_page", fake_list_recent_messages_page
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda _service, _mid: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "発送されました"},
                    {"name": "From", "value": "Amazon.co.jp <auto@amazon.co.jp>"},
                ]
            },
            "snippet": "your order",
        },
    )

    notifications: list[str] = []
    alerts: list[str] = []
    recoveries: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_notification",
        lambda **kwargs: notifications.append(str(kwargs.get("url"))) or True,
    )
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda _webhook, message, **_kwargs: alerts.append(message) or True,
    )
    monkeypatch.setattr(
        notifier,
        "send_discord_recovery",
        lambda _webhook, message, **_kwargs: recoveries.append(message) or True,
    )

    monkeypatch.setattr(
        gmail_client,
        "_send_discord_alert_with_dedupe",
        lambda _webhook, message, **_kwargs: alerts.append(message) or True,
    )
    monkeypatch.setattr(
        gmail_client,
        "_send_discord_recovery_with_dedupe",
        lambda _webhook, message, **_kwargs: recoveries.append(message) or True,
    )
    r1 = notifier.run_once(runtime)
    assert r1.failure_kind is None
    assert r1.notified_count == 1

    r2 = notifier.run_once(runtime)
    assert r2.failure_kind is not None
    assert len(alerts) == 1

    r3 = notifier.run_once(runtime)
    assert r3.failure_kind is None
    assert len(recoveries) >= 1

    state = _read_json(runtime.state_file)
    transient_state = _read_json(runtime.transient_state_file)
    assert state.get("active_incident_kind") is None
    assert transient_state.get("transient_network_issue_active") is False
    assert notifications
