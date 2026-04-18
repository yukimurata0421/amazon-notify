"""Tests added per code-review #14 — fills coverage gaps identified during
the second review cycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amazon_notify import discord_client, gmail_client, notification_bridge, notifier
from amazon_notify.backoff import retry_with_backoff
from amazon_notify.checkpoint_store import JsonlCheckpointStore
from amazon_notify.domain import AuthStatus, Checkpoint
from amazon_notify.gmail_source import GmailClientAdapter, GmailMailSource
from amazon_notify.runtime import mask_webhook_url

# ── #14-a  discord_client: non-retryable request exception returns False ──


def test_post_webhook_returns_false_on_non_retryable_request_exception(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        discord_client._SESSION,
        "post",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            discord_client.requests.exceptions.InvalidURL("bad url")
        ),
    )
    assert not discord_client._post_webhook("not-a-url", "hello", max_attempts=3)


# ── #14-b  checkpoint_store: partial JSONL line recovery ──


def test_checkpoint_store_tolerates_truncated_jsonl_line(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    runs_file = tmp_path / "runs.jsonl"
    state_file = tmp_path / "state.json"
    state_file.write_text("{}", encoding="utf-8")

    store = JsonlCheckpointStore(
        state_file=state_file,
        events_file=events_file,
        runs_file=runs_file,
    )

    store.advance_checkpoint(Checkpoint(message_id="m-1"), "run-1")

    good_line = events_file.read_text(encoding="utf-8").splitlines()[-1]
    events_file.write_text(
        good_line + "\n" + '{"schema_version":1,"event":"checkpoint_advanced"' + "\n",
        encoding="utf-8",
    )

    rebuilt = store.rebuild_indexes()
    assert rebuilt["checkpoint_index"]


# ── #14-c  gmail_source.iter_new_messages: truncation at max_messages ──


def test_iter_new_messages_truncates_at_max_messages(monkeypatch) -> None:
    messages = [{"id": f"m-{i}"} for i in range(10)]

    adapter = GmailClientAdapter(
        get_gmail_service_with_status_fn=lambda **_: (object(), AuthStatus.READY),
        list_recent_messages_page_fn=lambda _svc, *, query, max_results, page_token=None: (
            (messages, None) if page_token is None else ([], None)
        ),
        get_message_detail_fn=lambda _svc, mid: {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "test"},
                    {"name": "From", "value": "x@example.com"},
                ]
            },
            "snippet": "s",
        },
    )
    source = GmailMailSource(
        discord_webhook_url="https://discord.invalid/webhook",
        state={},
        state_file=Path("/tmp/state.json"),
        transient_state_file=Path("/tmp/transient_state.json"),
        dry_run=False,
        gmail_api_max_retries=1,
        gmail_api_base_delay_seconds=1.0,
        gmail_api_max_delay_seconds=1.0,
        runtime_paths=None,  # type: ignore[arg-type]
        transient_alert_min_duration_seconds=600.0,
        transient_alert_cooldown_seconds=1800.0,
        gmail_client=adapter,
    )
    envelopes = list(
        source.iter_new_messages(Checkpoint(message_id=None), max_messages=3)
    )
    assert len(envelopes) == 3


# ── #14-d  incident cache removal ──


def test_notifier_removes_incident_memory_cache_global() -> None:
    assert not hasattr(notifier, "_INCIDENT_MEMORY_MAP")


# ── #11  transient alert threshold behavior ──


def test_transient_issue_suppresses_before_threshold(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "state.json"
    state = {"last_message_id": "x"}

    alerts: list[str] = []
    monkeypatch.setattr("amazon_notify.gmail_transient_state.time.time", lambda: 1000.0)
    monkeypatch.setattr(
        notification_bridge,
        "send_discord_alert",
        lambda _w, message, **_kwargs: alerts.append(message) or True,
    )

    sent = gmail_client.record_transient_issue(
        state,
        state_file,
        TimeoutError("timed out"),
        webhook_url="https://discord.invalid/webhook",
        alert_message="transient issue",
        min_alert_duration_seconds=60.0,
        alert_cooldown_seconds=60.0,
    )
    assert not sent
    assert alerts == []


# ── #10  mask_webhook_url ──


def test_mask_webhook_url_redacts_path_and_token() -> None:
    masked = mask_webhook_url(
        "https://discord.com/api/webhooks/1234567/ABCDefgh_secret"
    )
    assert "ABCDefgh_secret" not in masked
    assert "discord.com" in masked
    assert "redacted" in masked


# ── #2  retry_with_backoff ──


def test_retry_with_backoff_succeeds_after_transient_failure() -> None:
    calls = {"count": 0}

    def _fn() -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("timed out")
        return "ok"

    result = retry_with_backoff(
        _fn,
        max_attempts=3,
        base_delay=0.001,
        max_delay=1.0,
        should_retry=lambda exc: isinstance(exc, TimeoutError),
    )
    assert result == "ok"
    assert calls["count"] == 2


def test_retry_with_backoff_raises_on_non_retryable() -> None:
    def _fn() -> str:
        raise ValueError("bad")

    with pytest.raises(ValueError):
        retry_with_backoff(
            _fn,
            max_attempts=3,
            base_delay=0.001,
            max_delay=1.0,
            should_retry=lambda exc: isinstance(exc, TimeoutError),
        )


def test_retry_with_backoff_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError):
        retry_with_backoff(
            lambda: None,
            max_attempts=0,
            base_delay=1.0,
            max_delay=1.0,
            should_retry=lambda _: True,
        )
