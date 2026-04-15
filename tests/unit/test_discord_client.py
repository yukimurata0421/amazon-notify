import json
import threading
from pathlib import Path

import pytest

from amazon_notify import discord_client


class _DummyResponse:
    def __init__(
        self,
        should_raise: bool = False,
        status_code: int | None = None,
        headers: dict | None = None,
    ):
        self._should_raise = should_raise
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def raise_for_status(self) -> None:
        if self._should_raise:
            raise RuntimeError("http error")


@pytest.fixture
def dedupe_state_path(tmp_path: Path) -> Path:
    return tmp_path / ".discord_dedupe_state.json"


def test_post_webhook_success(monkeypatch) -> None:
    calls: list[tuple[str, dict, int]] = []

    def fake_post(url: str, json: dict, timeout: int):
        calls.append((url, json, timeout))
        return _DummyResponse()

    monkeypatch.setattr(discord_client.requests, "post", fake_post)

    assert discord_client._post_webhook("https://discord.invalid/webhook", "hello")
    assert calls[0][0] == "https://discord.invalid/webhook"
    assert calls[0][2] == 10


def test_post_webhook_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        discord_client.requests,
        "post",
        lambda *args, **kwargs: _DummyResponse(should_raise=True),
    )

    assert not discord_client._post_webhook("https://discord.invalid/webhook", "hello")


def test_post_webhook_retries_on_rate_limit(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _DummyResponse(status_code=429, headers={"Retry-After": "1"})
        return _DummyResponse(status_code=204)

    sleeps: list[float] = []
    monkeypatch.setattr(discord_client.requests, "post", fake_post)
    monkeypatch.setattr(discord_client.time, "sleep", lambda sec: sleeps.append(sec))

    assert discord_client._post_webhook(
        "https://discord.invalid/webhook", "hello", max_attempts=3
    )
    assert calls["count"] == 2
    assert sleeps == [1.0]


def test_post_webhook_retries_on_transient_request_exception(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise discord_client.requests.exceptions.Timeout("timed out")
        return _DummyResponse(status_code=204)

    sleeps: list[float] = []
    monkeypatch.setattr(discord_client.requests, "post", fake_post)
    monkeypatch.setattr(discord_client.time, "sleep", lambda sec: sleeps.append(sec))

    assert discord_client._post_webhook(
        "https://discord.invalid/webhook",
        "hello",
        max_attempts=3,
        base_delay_seconds=0.5,
        max_delay_seconds=10.0,
    )
    assert calls["count"] == 2
    assert sleeps == [0.5]


def test_send_discord_functions_return_false_when_webhook_missing() -> None:
    assert not discord_client.send_discord_alert("", "alert")
    assert not discord_client.send_discord_recovery("", "recovery")
    assert not discord_client.send_discord_test("", "test")


def test_send_discord_alert_formats_message(
    monkeypatch, dedupe_state_path: Path
) -> None:
    payloads: list[str] = []
    monkeypatch.setattr(
        discord_client,
        "_post_webhook",
        lambda webhook_url, content, **_kwargs: payloads.append(content) or True,
    )

    assert discord_client.send_discord_alert(
        "https://discord.invalid/webhook",
        "warn",
        dedupe_state_path=dedupe_state_path,
    )
    assert payloads
    assert "Gmail監視システム警告" in payloads[0]


def test_send_discord_notification_success_and_failure(
    monkeypatch, dedupe_state_path: Path
) -> None:
    now = {"value": 1000.0}
    monkeypatch.setattr(discord_client.time, "time", lambda: now["value"])

    monkeypatch.setattr(discord_client, "_post_webhook", lambda *_args, **_kwargs: True)
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/1",
        dedupe_state_path=dedupe_state_path,
    )

    now["value"] = 1000.0 + discord_client._DEDUPE_WINDOW_SECONDS["notification"] + 1.0
    monkeypatch.setattr(
        discord_client, "_post_webhook", lambda *_args, **_kwargs: False
    )
    assert not discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/1",
        dedupe_state_path=dedupe_state_path,
    )


def test_send_discord_alert_suppresses_duplicate_within_window(
    monkeypatch, dedupe_state_path: Path
) -> None:
    posted: list[str] = []

    def fake_post(_webhook_url: str, content: str, **_kwargs) -> bool:
        posted.append(content)
        return True

    now = {"value": 1000.0}
    monkeypatch.setattr(discord_client, "_post_webhook", fake_post)
    monkeypatch.setattr(discord_client.time, "time", lambda: now["value"])

    assert discord_client.send_discord_alert(
        "https://discord.invalid/webhook",
        "warn",
        dedupe_state_path=dedupe_state_path,
    )
    now["value"] = 1005.0
    assert discord_client.send_discord_alert(
        "https://discord.invalid/webhook",
        "warn",
        dedupe_state_path=dedupe_state_path,
    )
    assert len(posted) == 1


def test_send_discord_notification_allows_different_message_ids(
    monkeypatch, dedupe_state_path: Path
) -> None:
    posted: list[str] = []

    def fake_post(_webhook_url: str, content: str, **_kwargs) -> bool:
        posted.append(content)
        return True

    now = {"value": 2000.0}
    monkeypatch.setattr(discord_client, "_post_webhook", fake_post)
    monkeypatch.setattr(discord_client.time, "time", lambda: now["value"])

    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/mail/u/0/#inbox/msg-1",
        dedupe_state_path=dedupe_state_path,
    )
    now["value"] = 2001.0
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/mail/u/0/#inbox/msg-1",
        dedupe_state_path=dedupe_state_path,
    )
    now["value"] = 2002.0
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/mail/u/0/#inbox/msg-2",
        dedupe_state_path=dedupe_state_path,
    )
    assert len(posted) == 2


def test_send_discord_alert_suppresses_when_inflight_claim_exists(
    monkeypatch, dedupe_state_path: Path
) -> None:
    state_path = dedupe_state_path
    dedupe_key = discord_client._build_dedupe_key(
        "alert",
        "⚠️ **Gmail監視システム警告**\nwarn",
    )
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": {
                    dedupe_key: {
                        "inflight_owner": "other-process",
                        "inflight_until": 1500.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    posted: list[str] = []

    def fake_post(_webhook_url: str, content: str, **_kwargs) -> bool:
        posted.append(content)
        return True

    monkeypatch.setattr(discord_client, "_post_webhook", fake_post)
    monkeypatch.setattr(discord_client.time, "time", lambda: 1400.0)

    assert discord_client.send_discord_alert(
        "https://discord.invalid/webhook",
        "warn",
        dedupe_state_path=dedupe_state_path,
    )
    assert posted == []


def test_post_webhook_rejects_invalid_max_attempts() -> None:
    with pytest.raises(ValueError):
        discord_client._post_webhook(
            "https://discord.invalid/webhook",
            "hello",
            max_attempts=0,
        )


def test_post_webhook_returns_false_on_non_retryable_status(monkeypatch) -> None:
    monkeypatch.setattr(
        discord_client.requests,
        "post",
        lambda *_args, **_kwargs: _DummyResponse(status_code=400),
    )
    assert not discord_client._post_webhook("https://discord.invalid/webhook", "hello")


def test_send_discord_recovery_and_test_format(
    monkeypatch, dedupe_state_path: Path
) -> None:
    payloads: list[str] = []
    monkeypatch.setattr(
        discord_client,
        "_post_webhook",
        lambda _webhook_url, content, **_kwargs: payloads.append(content) or True,
    )

    assert discord_client.send_discord_recovery(
        "https://discord.invalid/webhook",
        "recovered",
        dedupe_state_path=dedupe_state_path,
    )
    assert discord_client.send_discord_test(
        "https://discord.invalid/webhook",
        "test",
        dedupe_state_path=dedupe_state_path,
    )
    assert payloads[0].startswith("✅ **Gmail監視システム復旧**")
    assert payloads[1].startswith("🧪 **Amazon Notify テスト通知**")


def test_read_dedupe_entries_handles_corrupted_or_invalid_payload(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "bad.json"
    state_path.write_text("{invalid-json", encoding="utf-8")
    assert discord_client._read_dedupe_entries(state_path) == {}

    state_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    assert discord_client._read_dedupe_entries(state_path) == {}


def test_read_and_prune_dedupe_entries_variants(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "entries": {
                    "k1": 123,
                    "k2": {
                        "last_sent_at": "x",
                        "inflight_until": "y",
                        "inflight_owner": "",
                    },
                    "k2b": {"inflight_owner": "dangling-owner"},
                    "k3": {
                        "last_sent_at": 2_999_900.0,
                        "inflight_until": 150.0,
                        "inflight_owner": "p1",
                    },
                    "k4": {"inflight_until": 80.0, "inflight_owner": "p2"},
                    "k5": {"last_sent_at": 0.0},
                }
            }
        ),
        encoding="utf-8",
    )
    entries = discord_client._read_dedupe_entries(state_path)
    assert entries["k1"]["last_sent_at"] == 123.0
    assert "k2" not in entries
    assert "k2b" not in entries
    assert entries["k3"]["inflight_owner"] == "p1"

    changed = discord_client._prune_dedupe_entries(entries, now_epoch=3_000_000.0)
    assert changed
    assert "inflight_until" not in entries["k3"]
    assert "inflight_owner" not in entries["k3"]
    assert "k4" not in entries
    assert "k5" not in entries


def test_reserve_and_finalize_dedupe_claim_edge_paths(
    monkeypatch, tmp_path: Path
) -> None:
    original_lock = discord_client._discord_dedupe_lock
    state_file = tmp_path / ".discord_dedupe_state.json"
    assert discord_client._reserve_dedupe_claim(
        notification_kind="alert",
        content="x",
        dedupe_state_path=state_file,
        dedupe_window_seconds=0,
    ) == (True, None, None, None)

    class _BrokenLock:
        def __enter__(self):
            raise OSError("lock failed")

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(
        discord_client, "_discord_dedupe_lock", lambda _p: _BrokenLock()
    )
    allowed, state_path, dedupe_key, owner = discord_client._reserve_dedupe_claim(
        notification_kind="alert",
        content="x",
        dedupe_state_path=state_file,
        dedupe_window_seconds=60.0,
    )
    assert allowed and state_path is None and dedupe_key is None and owner is None

    # finalize no-op paths
    discord_client._finalize_dedupe_claim(
        state_path=None, dedupe_key=None, owner=None, sent=True
    )
    state_file.write_text(
        json.dumps(
            {"entries": {"key": {"inflight_owner": "other", "inflight_until": 9999.0}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(discord_client, "_discord_dedupe_lock", original_lock)
    discord_client._finalize_dedupe_claim(
        state_path=state_file,
        dedupe_key="key",
        owner="mine",
        sent=True,
    )


def test_post_webhook_with_dedupe_suppresses_concurrent_duplicate(
    monkeypatch, dedupe_state_path: Path
) -> None:
    call_started = threading.Event()
    allow_finish = threading.Event()
    call_count = {"count": 0}

    def fake_post_webhook(*_args, **_kwargs) -> bool:
        call_count["count"] += 1
        call_started.set()
        allow_finish.wait(timeout=1.0)
        return True

    monkeypatch.setattr(discord_client, "_post_webhook", fake_post_webhook)

    first_result: dict[str, bool] = {}

    def _first_send() -> None:
        first_result["sent"] = discord_client._post_webhook_with_dedupe(
            webhook_url="https://discord.invalid/webhook",
            content="same-content",
            notification_kind="alert",
            dedupe_state_path=dedupe_state_path,
            dedupe_window_seconds=600.0,
        )

    first_thread = threading.Thread(target=_first_send)
    first_thread.start()
    call_started.wait(timeout=1.0)

    second_sent = discord_client._post_webhook_with_dedupe(
        webhook_url="https://discord.invalid/webhook",
        content="same-content",
        notification_kind="alert",
        dedupe_state_path=dedupe_state_path,
        dedupe_window_seconds=600.0,
    )
    allow_finish.set()
    first_thread.join(timeout=1.0)

    assert first_result["sent"] is True
    assert second_sent is True
    assert call_count["count"] == 1
    monkeypatch.setattr(
        discord_client, "_discord_dedupe_lock", lambda _p: _BrokenLock()
    )
    discord_client._finalize_dedupe_claim(
        state_path=state_file,
        dedupe_key="key",
        owner="mine",
        sent=True,
    )
