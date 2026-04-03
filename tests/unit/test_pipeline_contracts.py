import json
from pathlib import Path

from amazon_notify import notifier
from amazon_notify.domain import AuthStatus, FailureKind


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _runtime(tmp_path: Path, *, dry_run: bool = False) -> dict:
    return {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_pattern": r"amazon\.co\.jp",
        "state_file": tmp_path / "state.json",
        "events_file": tmp_path / "events.jsonl",
        "runs_file": tmp_path / "runs.jsonl",
        "max_messages": 10,
        "subject_pattern": None,
        "dry_run": dry_run,
    }


def test_contract_checkpoint_advances_only_when_notification_succeeds(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime["state_file"].write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(notifier, "list_recent_messages", lambda *_args, **_kwargs: [{"id": "new-id"}, {"id": "old-id"}])
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {"name": "From", "value": "Amazon.co.jp <order-update@amazon.co.jp>"},
                ]
            },
            "snippet": "配達済みのお知らせ",
        },
    )
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: True)

    result = notifier.run_once(runtime)

    assert result.failure_kind is None
    assert _read_json(runtime["state_file"])["last_message_id"] == "new-id"
    events = _read_jsonl(runtime["events_file"])
    assert any(event["event"] == "checkpoint_advanced" for event in events)


def test_ordered_frontier_delivery_failure_stops_frontier_advancement(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime["state_file"].write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(notifier, "list_recent_messages", lambda *_args, **_kwargs: [{"id": "new-id"}, {"id": "old-id"}])
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {"name": "From", "value": "Amazon.co.jp <order-update@amazon.co.jp>"},
                ]
            },
            "snippet": "配達済みのお知らせ",
        },
    )
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: False)
    monkeypatch.setattr(notifier, "send_discord_alert", lambda *_args, **_kwargs: True)

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.DELIVERY_FAILED
    assert _read_json(runtime["state_file"])["last_message_id"] == "old-id"
    events = _read_jsonl(runtime["events_file"])
    assert any(event["event"] == "delivery_failed" for event in events)
    assert not any(
        event["event"] == "checkpoint_advanced" and event.get("source") == "pipeline_commit"
        for event in events
    )


def test_ordered_frontier_message_detail_failure_preserves_frontier(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime["state_file"].write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(notifier, "list_recent_messages", lambda *_args, **_kwargs: [{"id": "new-id"}, {"id": "old-id"}])
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("detail failed")),
    )

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.MESSAGE_DETAIL_FAILED
    assert _read_json(runtime["state_file"])["last_message_id"] == "old-id"
    events = _read_jsonl(runtime["events_file"])
    assert any(event["event"] == "message_detail_failed" for event in events)


def test_contract_auth_failure_records_auth_failed_event(monkeypatch, tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime["state_file"].write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: None)
    monkeypatch.setattr(notifier, "get_last_auth_status", lambda: AuthStatus.TOKEN_MISSING)

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.AUTH_FAILED
    assert result.auth_status == AuthStatus.TOKEN_MISSING
    events = _read_jsonl(runtime["events_file"])
    assert any(event["event"] == "auth_failed" for event in events)


def test_ordered_frontier_stops_processing_newer_messages_after_midstream_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime["state_file"].write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    # Gmail list は新しい順を想定。
    monkeypatch.setattr(
        notifier,
        "list_recent_messages",
        lambda *_args, **_kwargs: [
            {"id": "msg-c"},
            {"id": "msg-b"},
            {"id": "msg-a"},
            {"id": "old-id"},
        ],
    )
    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())

    def fake_detail(_service, message_id: str) -> dict:
        if message_id == "msg-b":
            raise RuntimeError("detail failed at midstream")
        return {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {"name": "From", "value": "Amazon.co.jp <order-update@amazon.co.jp>"},
                ]
            },
            "snippet": f"snippet-{message_id}",
        }

    monkeypatch.setattr(notifier, "get_message_detail", fake_detail)
    sent_ids: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_notification",
        lambda **kwargs: sent_ids.append(kwargs["url"].split("/")[-1]) or True,
    )

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.MESSAGE_DETAIL_FAILED
    # msg-a は送信済み、msg-b で停止し、msg-c は処理されない。
    assert sent_ids == ["msg-a"]
    assert _read_json(runtime["state_file"])["last_message_id"] == "msg-a"


def test_incident_lifecycle_suppresses_repeated_same_failure_and_recovers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime["state_file"].write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(notifier, "get_gmail_service", lambda **_: object())
    monkeypatch.setattr(notifier, "list_recent_messages", lambda *_args, **_kwargs: [{"id": "new-id"}, {"id": "old-id"}])
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: テスト注文"},
                    {"name": "From", "value": "Amazon.co.jp <order-update@amazon.co.jp>"},
                ]
            },
            "snippet": "配達済みのお知らせ",
        },
    )

    alerts: list[str] = []
    recoveries: list[str] = []
    monkeypatch.setattr(notifier, "send_discord_alert", lambda _w, m: alerts.append(m) or True)
    monkeypatch.setattr(notifier, "send_discord_recovery", lambda _w, m: recoveries.append(m) or True)

    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: False)
    notifier.run_once(runtime)
    notifier.run_once(runtime)

    state_after_failures = _read_json(runtime["state_file"])
    assert state_after_failures["active_incident_kind"] == "delivery_failed"
    assert state_after_failures["incident_suppressed_count"] == 1
    assert len(alerts) == 1

    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: True)
    notifier.run_once(runtime)

    state_after_recovery = _read_json(runtime["state_file"])
    assert "active_incident_kind" not in state_after_recovery
    assert len(recoveries) == 1
