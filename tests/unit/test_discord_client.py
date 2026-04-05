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
        lambda webhook_url, content: payloads.append(content) or True,
    )

    assert discord_client.send_discord_alert("https://discord.invalid/webhook", "warn")
    assert payloads
    assert "Gmail監視システム警告" in payloads[0]


def test_send_discord_notification_success_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(discord_client, "_post_webhook", lambda *_args, **_kwargs: True)
    assert discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/1",
    )

    monkeypatch.setattr(discord_client, "_post_webhook", lambda *_args, **_kwargs: False)
    assert not discord_client.send_discord_notification(
        webhook_url="https://discord.invalid/webhook",
        subject="件名",
        from_addr="from@example.com",
        snippet="snippet",
        url="https://mail.google.com/1",
    )
