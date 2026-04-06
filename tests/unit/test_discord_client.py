import json
from pathlib import Path

import pytest

from amazon_notify import discord_client


class _DummyResponse:
    def __init__(self, should_raise: bool = False, status_code: int | None = None, headers: dict | None = None):
        self._should_raise = should_raise
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def raise_for_status(self) -> None:
        if self._should_raise:
            raise RuntimeError("http error")


@pytest.fixture(autouse=True)
def _isolate_dedupe_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        discord_client,
        "_discord_dedupe_state_path",
        lambda: tmp_path / ".discord_dedupe_state.json",
    )


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

    assert discord_client._post_webhook("https://discord.invalid/webhook", "hello", max_attempts=3)
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


def test_send_discord_alert_formats_message(monkeypatch) -> None:
    payloads: list[str] = []
    monkeypatch.setattr(
        discord_client,
        "_post_webhook",
        lambda webhook_url, content, **_kwargs: payloads.append(content) or True,
    )

    assert discord_client.send_discord_alert("https://discord.invalid/webhook", "warn")
    assert payloads
    assert "Gmail監視システム警告" in payloads[0]


def test_send_discord_notification_success_and_failure(monkeypatch) -> None:
    now = {"value": 1000.0}
    monkeypatch.setattr(discord_client.time, "time", lambda: now["value"])

    monkeypatch.setattr(discord_client, "_post_webhook", lambda *_args, **_kwargs: True)
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/1",
    )

    now["value"] = 1000.0 + discord_client._DEDUPE_WINDOW_SECONDS["notification"] + 1.0
    monkeypatch.setattr(discord_client, "_post_webhook", lambda *_args, **_kwargs: False)
    assert not discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/1",
    )


def test_send_discord_alert_suppresses_duplicate_within_window(monkeypatch) -> None:
    posted: list[str] = []

    def fake_post(_webhook_url: str, content: str, **_kwargs) -> bool:
        posted.append(content)
        return True

    now = {"value": 1000.0}
    monkeypatch.setattr(discord_client, "_post_webhook", fake_post)
    monkeypatch.setattr(discord_client.time, "time", lambda: now["value"])

    assert discord_client.send_discord_alert("https://discord.invalid/webhook", "warn")
    now["value"] = 1005.0
    assert discord_client.send_discord_alert("https://discord.invalid/webhook", "warn")
    assert len(posted) == 1


def test_send_discord_notification_allows_different_message_ids(monkeypatch) -> None:
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
    )
    now["value"] = 2001.0
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/mail/u/0/#inbox/msg-1",
    )
    now["value"] = 2002.0
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/mail/u/0/#inbox/msg-2",
    )
    assert len(posted) == 2


def test_send_discord_alert_suppresses_when_inflight_claim_exists(monkeypatch) -> None:
    state_path = discord_client._discord_dedupe_state_path()
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

    assert discord_client.send_discord_alert("https://discord.invalid/webhook", "warn")
    assert posted == []
