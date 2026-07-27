from __future__ import annotations

import json

from amazon_notify import notifier
from tests.unit.notifier_test_helpers import (
    build_runtime,
    patch_gmail_ready,
    single_page,
)


def _events(runtime) -> list[dict]:
    if not runtime.events_file.exists():
        return []
    return [
        json.loads(line)
        for line in runtime.events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_first_run_skips_existing_and_sends_one_setup_notification(
    monkeypatch, tmp_path
) -> None:
    runtime = build_runtime(tmp_path)
    patch_gmail_ready(monkeypatch)
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "latest"}, {"id": "older"}]),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing messages must not be decoded")
        ),
    )
    setup_messages: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_test",
        lambda _url, message, **_kwargs: setup_messages.append(message) or True,
    )

    result = notifier.run_once(runtime)

    assert result.processed_count == 0
    assert result.notified_count == 0
    assert result.checkpoint_after == "latest"
    assert setup_messages == [
        "セットアップが完了しました。\n"
        "Gmail接続: 正常\n"
        "Discord通知: 正常\n"
        "初期同期: 既存メールを通知対象外として初期化\n"
        "これ以降に届いた対象メールを通知します。"
    ]
    assert [event["event"] for event in _events(runtime)] == [
        "initial_sync_completed",
        "initial_sync_notification_sent",
    ]


def test_message_arriving_after_initial_sync_is_notified(monkeypatch, tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    patch_gmail_ready(monkeypatch)
    pages = iter(
        [
            ([{"id": "initial"}], None),
            ([{"id": "initial"}], None),
            ([{"id": "new"}, {"id": "initial"}], None),
        ]
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        lambda *_args, **_kwargs: next(pages),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: 新着"},
                    {
                        "name": "From",
                        "value": "Amazon.co.jp <order-update@amazon.co.jp>",
                    },
                ]
            },
            "snippet": "new",
        },
    )
    setup_calls: list[str] = []
    notification_calls: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_test",
        lambda _url, message, **_kwargs: setup_calls.append(message) or True,
    )
    monkeypatch.setattr(
        notifier,
        "send_discord_notification",
        lambda **kwargs: notification_calls.append(kwargs["subject"]) or True,
    )

    notifier.run_once(runtime)
    result = notifier.run_once(runtime)

    assert result.notified_count == 1
    assert result.checkpoint_after == "new"
    assert len(setup_calls) == 1
    assert notification_calls == ["配達済み: 新着"]


def test_empty_inbox_is_initialized_and_first_future_message_is_not_skipped(
    monkeypatch, tmp_path
) -> None:
    runtime = build_runtime(tmp_path)
    patch_gmail_ready(monkeypatch)
    pages = iter(
        [
            ([], None),
            ([], None),
            ([{"id": "first"}], None),
        ]
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        lambda *_args, **_kwargs: next(pages),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: 初回新着"},
                    {"name": "From", "value": "Amazon.co.jp <x@amazon.co.jp>"},
                ]
            },
            "snippet": "first",
        },
    )
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: True)

    first = notifier.run_once(runtime)
    second = notifier.run_once(runtime)

    assert first.processed_count == 0
    assert second.notified_count == 1
    assert second.checkpoint_after == "first"
    initial_events = [
        event
        for event in _events(runtime)
        if event["event"] == "initial_sync_completed"
    ]
    assert len(initial_events) == 1
    assert initial_events[0]["checkpoint"] is None


def test_failed_setup_notification_is_retried_without_reprocessing_existing(
    monkeypatch, tmp_path
) -> None:
    runtime = build_runtime(tmp_path)
    patch_gmail_ready(monkeypatch)
    pages = iter(
        [
            ([{"id": "initial"}], None),
            ([{"id": "initial"}], None),
            ([{"id": "initial"}], None),
        ]
    )
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        lambda *_args, **_kwargs: next(pages),
    )
    setup_results = iter([False, True])
    setup_calls: list[bool] = []

    def fake_setup(*_args, **_kwargs) -> bool:
        result = next(setup_results)
        setup_calls.append(result)
        return result

    monkeypatch.setattr(notifier, "send_discord_test", fake_setup)

    first = notifier.run_once(runtime)
    second = notifier.run_once(runtime)

    assert first.processed_count == 0
    assert second.processed_count == 0
    assert setup_calls == [False, True]
    assert (
        sum(
            event["event"] == "initial_sync_notification_sent"
            for event in _events(runtime)
        )
        == 1
    )


def test_existing_installation_with_null_checkpoint_is_not_treated_as_first_run(
    monkeypatch, tmp_path
) -> None:
    runtime = build_runtime(tmp_path)
    runtime.state_file.write_text(
        json.dumps({"last_message_id": None}),
        encoding="utf-8",
    )
    patch_gmail_ready(monkeypatch)
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "existing"}]),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: 既存環境"},
                    {"name": "From", "value": "Amazon.co.jp <x@amazon.co.jp>"},
                ]
            },
            "snippet": "existing installation",
        },
    )
    setup_calls: list[str] = []
    notification_calls: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_test",
        lambda *_args, **_kwargs: setup_calls.append("sent") or True,
    )
    monkeypatch.setattr(
        notifier,
        "send_discord_notification",
        lambda **kwargs: notification_calls.append(kwargs["subject"]) or True,
    )

    result = notifier.run_once(runtime)

    assert result.notified_count == 1
    assert result.checkpoint_after == "existing"
    assert setup_calls == []
    assert notification_calls == ["配達済み: 既存環境"]
    assert not any(
        event["event"].startswith("initial_sync_") for event in _events(runtime)
    )


def test_backfill_remains_explicit_opt_in(monkeypatch, tmp_path) -> None:
    runtime = build_runtime(tmp_path, initial_sync_mode="backfill")
    patch_gmail_ready(monkeypatch)
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "existing"}]),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: 過去メール"},
                    {"name": "From", "value": "Amazon.co.jp <x@amazon.co.jp>"},
                ]
            },
            "snippet": "existing",
        },
    )
    monkeypatch.setattr(notifier, "send_discord_notification", lambda **_kwargs: True)

    result = notifier.run_once(runtime)

    assert result.notified_count == 1
    assert result.checkpoint_after == "existing"
    initial_event = _events(runtime)[0]
    assert initial_event["event"] == "initial_sync_completed"
    assert initial_event["mode"] == "backfill"


def test_dry_run_does_not_initialize_or_send_setup_notification(
    monkeypatch, tmp_path
) -> None:
    runtime = build_runtime(tmp_path, dry_run=True)
    patch_gmail_ready(monkeypatch)
    monkeypatch.setattr(
        notifier,
        "list_recent_messages_page",
        single_page([{"id": "existing"}]),
    )
    monkeypatch.setattr(
        notifier,
        "get_message_detail",
        lambda *_args, **_kwargs: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "配達済み: dry-run"},
                    {"name": "From", "value": "Amazon.co.jp <x@amazon.co.jp>"},
                ]
            },
            "snippet": "dry",
        },
    )
    setup_calls: list[str] = []
    monkeypatch.setattr(
        notifier,
        "send_discord_test",
        lambda *_args, **_kwargs: setup_calls.append("sent") or True,
    )

    result = notifier.run_once(runtime)

    assert result.notified_count == 1
    assert setup_calls == []
    assert not runtime.events_file.exists()
    state = (
        json.loads(runtime.state_file.read_text(encoding="utf-8"))
        if runtime.state_file.exists()
        else {}
    )
    assert "initial_sync" not in state
    assert not state.get("last_message_id")
