import json
from pathlib import Path

from amazon_notify import (
    config,
    gmail_client,
    gmail_transient_state,
    notification_bridge,
    notifier,
)
from tests.unit.notifier_test_helpers import build_runtime, single_page


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


def test_is_transient_network_error_for_library_exception_types(monkeypatch) -> None:
    class FakeLibraryConnectionError(Exception):
        pass

    monkeypatch.setattr(
        gmail_client,
        "_LIBRARY_TRANSIENT_EXCEPTION_TYPES",
        (FakeLibraryConnectionError,),
    )
    assert gmail_client.is_transient_network_error(
        FakeLibraryConnectionError("no keyword required")
    )


def test_mark_issue_and_notify_recovery(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state = {"last_message_id": "msg-1"}

    monkeypatch.setattr(
        notification_bridge, "send_discord_alert", lambda *_args, **_kwargs: True
    )
    gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=0.0,
        alert_cooldown_seconds=0.0,
    )
    saved = _read_json(state_file)
    assert saved["transient_network_issue_active"] is True
    assert "timed out" in saved["last_transient_error"]

    calls: list[tuple[str, str]] = []

    def fake_send_recovery(webhook_url: str, message: str, **_kwargs) -> None:
        calls.append((webhook_url, message))
        return True

    monkeypatch.setattr(
        notification_bridge, "send_discord_recovery", fake_send_recovery
    )

    gmail_client.notify_recovery_if_needed(
        "https://discord.invalid/webhook", state, state_file
    )

    assert len(calls) == 1
    assert "復旧" in calls[0][1]
    assert "timed out" in calls[0][1]

    saved_after = _read_json(state_file)
    assert saved_after["transient_network_issue_active"] is False
    assert "last_transient_error" not in saved_after


def test_notify_recovery_keeps_state_when_discord_send_fails(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state = {
        "last_message_id": "msg-1",
        "transient_network_issue_active": True,
        "transient_network_issue_notified": True,
        "last_transient_error": "timed out",
        "last_transient_error_at": "2026-04-02 10:00:00",
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(
        notification_bridge, "send_discord_recovery", lambda *_args, **_kwargs: False
    )

    gmail_client.notify_recovery_if_needed(
        "https://discord.invalid/webhook",
        state,
        state_file,
    )

    saved = _read_json(state_file)
    assert saved["transient_network_issue_active"] is True


def test_notify_recovery_silent_clear_when_transient_was_never_alerted(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state = {
        "last_message_id": "msg-1",
        "transient_network_issue_active": True,
        "last_transient_error": "timed out",
        "last_transient_error_at": "2026-04-02 10:00:00",
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(
        notification_bridge,
        "send_discord_recovery",
        lambda _webhook, message, **_kwargs: calls.append(message) or True,
    )
    gmail_client.notify_recovery_if_needed(
        "https://discord.invalid/webhook", state, state_file
    )

    assert calls == []
    saved = _read_json(state_file)
    assert saved["transient_network_issue_active"] is False


def test_notify_recovery_uses_latest_persisted_state(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    # Disk state is already recovered, but in-memory state is stale.
    state_file.write_text(
        json.dumps(
            {
                "last_message_id": "msg-1",
                "transient_network_issue_active": False,
            }
        ),
        encoding="utf-8",
    )
    stale_state = {
        "last_message_id": "msg-1",
        "transient_network_issue_active": True,
        "transient_network_issue_notified": True,
        "last_transient_error": "timed out",
        "last_transient_error_at": "2026-04-06 09:42:52",
    }

    calls: list[str] = []
    monkeypatch.setattr(
        notification_bridge,
        "send_discord_recovery",
        lambda _webhook, message, **_kwargs: calls.append(message) or True,
    )

    gmail_client.notify_recovery_if_needed(
        "https://discord.invalid/webhook",
        stale_state,
        state_file,
    )

    assert calls == []
    assert stale_state["transient_network_issue_active"] is False


def test_record_transient_issue_uses_latest_persisted_state_for_cooldown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "last_message_id": "msg-1",
                "transient_network_issue_active": True,
                "transient_network_issue_notified": True,
                "transient_network_issue_first_seen_at_epoch": 1000.0,
                "transient_network_issue_last_alert_at_epoch": 1090.0,
                "transient_network_issue_occurrences": 2,
            }
        ),
        encoding="utf-8",
    )
    stale_state = {"last_message_id": "msg-1"}

    alerts: list[str] = []
    monkeypatch.setattr(
        notification_bridge,
        "send_discord_alert",
        lambda _webhook, message, **_kwargs: alerts.append(message) or True,
    )
    monkeypatch.setattr(gmail_transient_state.time, "time", lambda: 1100.0)

    sent = gmail_client.record_transient_issue(
        stale_state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=0.0,
        alert_cooldown_seconds=60.0,
    )

    assert sent is False
    assert alerts == []
    saved = _read_json(state_file)
    assert saved["transient_network_issue_occurrences"] == 3


def test_save_state_creates_parent_directories(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "runtime" / "state.json"

    config.save_state(state_file, {"last_message_id": "msg-1"})

    assert _read_json(state_file)["last_message_id"] == "msg-1"


def test_run_once_sends_amazon_notification_and_updates_state(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
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
    assert sent[0]["dedupe_state_path"] == runtime.discord_dedupe_state_file

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "new-id"


def test_run_once_does_not_advance_state_when_notification_fails(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
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
        lambda webhook_url, message, **_kwargs: alerts.append(message) or True,
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert alerts


def test_run_once_dry_run_does_not_send_notification_or_update_state(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file, dry_run=True)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
    )

    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda service, message_id: {
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
        },
    )

    sent: list[dict] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_notification",
        lambda **kwargs: sent.append(kwargs) or True,
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert not sent


def test_run_once_advances_state_for_non_amazon_mail_and_logs_count(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "new-id"}, {"id": "old-id"}]),
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
    monkeypatch.setattr(
        notifier,
        "send_discord_notification",
        lambda **kwargs: sent.append(kwargs) or True,
    )

    logs: list[str] = []

    def fake_info(message: str, *args) -> None:
        logs.append(message % args if args else message)

    monkeypatch.setattr(notifier.LOGGER, "info", fake_info)

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "new-id"
    assert not sent
    assert any("non_amazon_skipped=1" in message for message in logs)


def test_run_once_marks_transient_issue_when_message_list_times_out(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "id-1"}), encoding="utf-8")

    runtime = build_runtime(
        tmp_path,
        state_file=state_file,
        transient_alert_min_duration_seconds=0.0,
        transient_alert_cooldown_seconds=0.0,
    )

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )

    def raise_timeout(
        _service, *, query: str, max_results: int, page_token: str | None = None
    ):
        assert query == "in:inbox"
        _ = max_results
        _ = page_token
        raise TimeoutError("timed out")

    monkeypatch.setattr(notifier, "list_recent_messages_page", raise_timeout)

    alerts: list[str] = []
    monkeypatch.setattr(
        notification_bridge,
        "send_discord_alert",
        lambda webhook_url, message, **_kwargs: alerts.append(message),
    )

    notifier.run_once(runtime)

    saved = _read_json(runtime.transient_state_file)
    assert saved["transient_network_issue_active"] is True
    assert "timed out" in saved["last_transient_error"]
    assert alerts


def test_run_once_handles_http_error_and_alerts(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )

    class DummyHttpError(Exception):
        pass

    monkeypatch.setattr(notifier, "HttpError", DummyHttpError)

    def raise_http_error(
        _service,
        *,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ):
        assert query == "in:inbox"
        _ = max_results
        _ = page_token
        raise DummyHttpError("http error")

    monkeypatch.setattr(notifier, "list_recent_messages_page", raise_http_error)

    alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda _webhook_url, message, **_kwargs: alerts.append(message) or True,
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert alerts
    assert "Gmail API" in alerts[0]


def test_run_once_breaks_when_message_detail_fetch_fails(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
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

    alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda _webhook_url, message, **_kwargs: alerts.append(message) or True,
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert alerts
    assert "メッセージ詳細の取得に失敗" in alerts[0]


def test_run_once_no_messages_logs_and_keeps_state(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file)

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (object(), notifier.AuthStatus.READY),
    )
    monkeypatch.setattr(notifier, "list_recent_messages_page", single_page([]))

    logs: list[str] = []

    def fake_info(message: str, *args) -> None:
        logs.append(message % args if args else message)

    monkeypatch.setattr(notifier.LOGGER, "info", fake_info)

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert any("RUN_ONCE_NO_MESSAGES" in message for message in logs)


def test_run_once_preserves_frontier_when_backlog_exceeds_max_messages(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file, max_messages=2)

    class _Service:
        def users(self):
            return object()

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (_Service(), notifier.AuthStatus.READY),
    )

    pages = {
        None: ([{"id": "m5"}, {"id": "m4"}, {"id": "m3"}], "page-2"),
        "page-2": ([{"id": "m2"}, {"id": "m1"}, {"id": "old-id"}], None),
    }

    def fake_list_recent_messages_page(
        _service, *, query: str, max_results: int, page_token: str | None = None
    ):
        assert query == "in:inbox"
        return pages[page_token]

    monkeypatch.setattr(
        notifier, "list_recent_messages_page", fake_list_recent_messages_page
    )

    def fake_message_detail(_service, message_id: str) -> dict:
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
            "snippet": f"snippet-{message_id}",
        }

    monkeypatch.setattr(notifier, "get_message_detail", fake_message_detail)

    sent_message_ids: list[str] = []

    def fake_send_notification(**kwargs) -> bool:
        sent_message_ids.append(kwargs["url"].rsplit("/", 1)[-1])
        return True

    monkeypatch.setattr(notifier, "send_discord_notification", fake_send_notification)

    notifier.run_once(runtime)

    # oldest-first frontier within backlog, capped by max_messages
    assert sent_message_ids == ["m1", "m2"]
    saved = _read_json(state_file)
    assert saved["last_message_id"] == "m2"


def test_run_once_fails_safe_when_checkpoint_not_found_in_paginated_listing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "old-id"}), encoding="utf-8")

    runtime = build_runtime(tmp_path, state_file=state_file, max_messages=2)

    class _Service:
        def users(self):
            return object()

    monkeypatch.setattr(
        notifier,
        "get_gmail_service_with_status",
        lambda **_: (_Service(), notifier.AuthStatus.READY),
    )

    pages = {
        None: ([{"id": "m3"}, {"id": "m2"}], "page-2"),
        "page-2": ([{"id": "m1"}], None),
    }

    def fake_list_recent_messages_page(
        _service, *, query: str, max_results: int, page_token: str | None = None
    ):
        assert query == "in:inbox"
        return pages[page_token]

    monkeypatch.setattr(
        notifier, "list_recent_messages_page", fake_list_recent_messages_page
    )

    sent_alerts: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_alert",
        lambda _webhook_url, message, **_kwargs: sent_alerts.append(message) or True,
    )

    notifier.run_once(runtime)

    saved = _read_json(state_file)
    assert saved["last_message_id"] == "old-id"
    assert sent_alerts
    assert "checkpoint_not_found_in_listing" in sent_alerts[0]


def test_report_unhandled_exception_persists_run_and_events(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(notifier, "send_discord_alert", lambda *_args, **_kwargs: True)

    result = notifier.report_unhandled_exception(runtime, RuntimeError("boom"))

    assert result.failure_kind == notifier.FailureKind.SOURCE_FAILED
    assert result.failure_message == "boom"

    events = [
        json.loads(line)
        for line in runtime.events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event.get("event") == "source_failed" for event in events)
    assert any(event.get("event") == "incident_opened" for event in events)

    runs = [
        json.loads(line)
        for line in runtime.runs_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(runs) == 1
    assert runs[0]["failure_kind"] == "source_failed"


def test_report_unhandled_exception_handles_persistence_failures(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = build_runtime(tmp_path, dry_run=True)

    class _BrokenStore:
        def load_checkpoint(self):
            raise RuntimeError("checkpoint load failed")

        def append_event(self, *_args, **_kwargs):
            raise OSError("event write failed")

        def append_run_result(self, *_args, **_kwargs):
            raise RuntimeError("run write failed")

        def load_incident_state(self):
            return None

    monkeypatch.setattr(
        notifier, "JsonlCheckpointStore", lambda *_args, **_kwargs: _BrokenStore()
    )

    result = notifier.report_unhandled_exception(
        runtime, RuntimeError("guard exploded")
    )

    assert result.failure_kind == notifier.FailureKind.SOURCE_FAILED
    assert result.failure_message == "guard exploded"
    assert result.checkpoint_before is None


def test_notifier_does_not_expose_incident_memory_cache() -> None:
    assert not hasattr(notifier, "_INCIDENT_MEMORY_MAP")
