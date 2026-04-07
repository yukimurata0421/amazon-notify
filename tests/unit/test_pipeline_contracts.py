import json
from pathlib import Path

from amazon_notify import notifier
from amazon_notify.domain import AuthStatus, FailureKind
from amazon_notify.errors import CheckpointError
from tests.unit.notifier_test_helpers import build_runtime, single_page


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_contract_checkpoint_advances_only_when_notification_succeeds(monkeypatch, tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )
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
    assert _read_json(runtime.state_file)["last_message_id"] == "new-id"
    events = _read_jsonl(runtime.events_file)
    assert any(event["event"] == "checkpoint_advanced" for event in events)


def test_ordered_frontier_delivery_failure_stops_frontier_advancement(monkeypatch, tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )
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
    assert _read_json(runtime.state_file)["last_message_id"] == "old-id"
    events = _read_jsonl(runtime.events_file)
    assert any(event["event"] == "delivery_failed" for event in events)
    assert not any(
        event["event"] == "checkpoint_advanced" and event.get("source") == "pipeline_commit"
        for event in events
    )


def test_ordered_frontier_message_detail_failure_preserves_frontier(monkeypatch, tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("detail failed")),
    )

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.MESSAGE_DETAIL_FAILED
    assert _read_json(runtime.state_file)["last_message_id"] == "old-id"
    events = _read_jsonl(runtime.events_file)
    assert any(event["event"] == "message_detail_failed" for event in events)


def test_contract_auth_failure_records_auth_failed_event(monkeypatch, tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (None, AuthStatus.TOKEN_MISSING),
    )

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.AUTH_FAILED
    assert result.auth_status == AuthStatus.TOKEN_MISSING
    events = _read_jsonl(runtime.events_file)
    assert any(event["event"] == "auth_failed" for event in events)


def test_ordered_frontier_stops_processing_newer_messages_after_midstream_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    # Gmail list は新しい順を想定。
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page(
            [
                {"id": "msg-c"},
                {"id": "msg-b"},
                {"id": "msg-a"},
                {"id": "old-id"},
            ]
        ),
    )
    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )

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
    assert _read_json(runtime.state_file)["last_message_id"] == "msg-a"


def test_incident_lifecycle_suppresses_repeated_same_failure_and_recovers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )
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

    alerts: list[tuple[str, Path | None]] = []
    recoveries: list[tuple[str, Path | None]] = []

    def fake_send_discord_alert(_webhook_url: str, message: str, **kwargs) -> bool:
        alerts.append((message, kwargs.get("dedupe_state_path")))
        return True

    def fake_send_discord_recovery(_webhook_url: str, message: str, **kwargs) -> bool:
        recoveries.append((message, kwargs.get("dedupe_state_path")))
        return True

    monkeypatch.setattr(notifier, "send_discord_alert", fake_send_discord_alert)
    monkeypatch.setattr(notifier, "send_discord_recovery", fake_send_discord_recovery)

    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: False)
    notifier.run_once(runtime)
    notifier.run_once(runtime)

    state_after_failures = _read_json(runtime.state_file)
    assert state_after_failures["active_incident_kind"] == "delivery_failed"
    assert state_after_failures["incident_suppressed_count"] == 1
    assert len(alerts) == 1
    assert alerts[0][1] == runtime.discord_dedupe_state_file

    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: True)
    notifier.run_once(runtime)

    state_after_recovery = _read_json(runtime.state_file)
    assert "active_incident_kind" not in state_after_recovery
    assert len(recoveries) == 1
    assert recoveries[0][1] == runtime.discord_dedupe_state_file


def test_run_once_marks_checkpoint_failed_when_run_result_persist_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )
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
    alerts: list[str] = []
    monkeypatch.setattr(notifier, "send_discord_alert", lambda _w, m, **_kwargs: alerts.append(m) or True)
    monkeypatch.setattr(
        notifier.JsonlCheckpointStore,
        "append_run_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(CheckpointError("run result 保存に失敗しました: disk full")),
    )

    result = notifier.run_once(runtime)

    assert result.failure_kind == FailureKind.CHECKPOINT_FAILED
    assert result.should_alert is True
    assert len(alerts) == 1


def test_run_once_does_not_crash_when_failure_event_persist_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")
    runtime.events_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event": "checkpoint_advanced",
                "run_id": "bootstrap",
                "at": "2026-04-05 00:00:00",
                "checkpoint": "old-id",
                "source": "seed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (None, AuthStatus.TOKEN_MISSING),
    )
    monkeypatch.setattr(
        notifier.JsonlCheckpointStore,
        "append_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("events disk full")),
    )
    monkeypatch.setattr(notifier, "send_discord_alert", lambda *_args, **_kwargs: True)

    result = notifier.run_once(runtime)
    assert result.failure_kind == FailureKind.AUTH_FAILED


def test_incident_memory_suppression_reduces_repeat_alert_when_incident_state_write_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )
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
    alerts: list[str] = []
    monkeypatch.setattr(notifier, "send_discord_alert", lambda _w, m, **_kwargs: alerts.append(m) or True)
    monkeypatch.setattr(
        notifier.JsonlCheckpointStore,
        "open_incident",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state disk full")),
    )
    monkeypatch.setattr(notifier.time, "time", lambda: 1_000.0)
    runtime.incident_memory_suppressed_until.clear()

    notifier.run_once(runtime)
    notifier.run_once(runtime)

    assert len(alerts) == 1
