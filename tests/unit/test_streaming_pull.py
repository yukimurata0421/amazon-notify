import json
import threading
import time
from pathlib import Path

import pytest

from amazon_notify import streaming_pull


class _DummyMessage:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload).encode("utf-8")
        self.message_id = "msg-1"
        self.publish_time = "2026-04-04T00:00:00Z"


def test_parse_pubsub_event_reads_history_and_email() -> None:
    msg = _DummyMessage({"emailAddress": "user@example.com", "historyId": "12345"})

    parsed = streaming_pull.parse_pubsub_event(msg)
    assert parsed.message_id == "msg-1"
    assert parsed.history_id == 12345
    assert parsed.email_address == "user@example.com"


def test_parse_pubsub_event_raises_for_invalid_payload() -> None:
    msg = _DummyMessage({"historyId": "not-an-int"})
    with pytest.raises(ValueError):
        streaming_pull.parse_pubsub_event(msg)


def test_touch_heartbeat_file_creates_and_updates_mtime(tmp_path) -> None:
    heartbeat_file = tmp_path / "runtime" / "heartbeat.txt"
    streaming_pull.touch_heartbeat_file(heartbeat_file)
    first = heartbeat_file.stat().st_mtime
    time.sleep(0.01)
    streaming_pull.touch_heartbeat_file(heartbeat_file)
    second = heartbeat_file.stat().st_mtime
    assert second >= first


def test_run_streaming_pull_raises_for_invalid_args() -> None:
    with pytest.raises(ValueError):
        streaming_pull.run_streaming_pull(
            subscription_path="projects/p/subscriptions/s",
            on_trigger=lambda: True,
            queue_size=0,
        )
    with pytest.raises(ValueError):
        streaming_pull.run_streaming_pull(
            subscription_path="projects/p/subscriptions/s",
            on_trigger=lambda: True,
            heartbeat_interval_seconds=0.0,
        )


def test_run_streaming_pull_logs_queue_size_alias_deprecation(monkeypatch) -> None:
    warnings: list[str] = []

    def fake_warning(message: str, *args) -> None:
        warnings.append(message % args if args else message)

    monkeypatch.setattr(streaming_pull.LOGGER, "warning", fake_warning)

    with pytest.raises(ValueError):
        streaming_pull.run_streaming_pull(
            subscription_path="projects/p/subscriptions/s",
            on_trigger=lambda: True,
            queue_size=32,
            heartbeat_interval_seconds=0.0,
        )

    assert any("PUBSUB_QUEUE_SIZE_ALIAS_DEPRECATED" in item for item in warnings)


def test_ensure_pubsub_dependencies_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(streaming_pull, "PUBSUB_IMPORT_ERROR", ImportError("missing"))
    with pytest.raises(ModuleNotFoundError):
        streaming_pull.ensure_pubsub_dependencies()


def test_run_streaming_pull_processes_messages_and_updates_heartbeat(monkeypatch, tmp_path: Path) -> None:
    class _FakeMessage:
        def __init__(self, payload: dict):
            self.data = json.dumps(payload).encode("utf-8")
            self.message_id = "msg-1"
            self.publish_time = "2026-04-04T00:00:00Z"
            self.acked = False

        def ack(self) -> None:
            self.acked = True

    trigger_seen = threading.Event()

    class _FakeFuture:
        def __init__(self, callback, messages):
            self._callback = callback
            self._messages = messages
            self.cancelled = False

        def result(self) -> None:
            for message in self._messages:
                self._callback(message)
            trigger_seen.wait(timeout=1.0)
            raise RuntimeError("stop stream")

        def cancel(self) -> None:
            self.cancelled = True

    class _FakeFlowControl:
        def __init__(self, max_messages: int):
            self.max_messages = max_messages

    class _FakeSubscriber:
        last_future = None
        closed = False

        def subscribe(self, subscription_path: str, callback, flow_control):
            assert subscription_path == "projects/p/subscriptions/s"
            assert flow_control.max_messages == 10
            future = _FakeFuture(
                callback,
                [
                    _FakeMessage({"emailAddress": "user@example.com", "historyId": "100"}),
                    _FakeMessage({"emailAddress": "user@example.com", "historyId": "101"}),
                ],
            )
            _FakeSubscriber.last_future = future
            return future

        def close(self) -> None:
            _FakeSubscriber.closed = True

    class _FakePubSub:
        class types:
            FlowControl = _FakeFlowControl

        SubscriberClient = _FakeSubscriber

    triggers: list[bool] = []
    heartbeat_file = tmp_path / "runtime" / "heartbeat.txt"
    monkeypatch.setattr(streaming_pull, "ensure_pubsub_dependencies", lambda: None)
    monkeypatch.setattr(streaming_pull, "pubsub_v1", _FakePubSub)

    with pytest.raises(RuntimeError, match="stop stream"):
        streaming_pull.run_streaming_pull(
            subscription_path="projects/p/subscriptions/s",
            on_trigger=lambda: trigger_seen.set() or triggers.append(True) or True,
            queue_size=32,
            flow_control_max_messages=10,
            heartbeat_file=heartbeat_file,
            heartbeat_interval_seconds=0.05,
        )

    assert triggers
    assert heartbeat_file.exists()
    assert _FakeSubscriber.last_future is not None
    assert _FakeSubscriber.last_future.cancelled is True
    assert _FakeSubscriber.closed is True


def test_run_streaming_pull_skips_invalid_message_payload(monkeypatch) -> None:
    class _BadMessage:
        def __init__(self):
            self.data = b"{not json"
            self.message_id = "msg-1"
            self.publish_time = "2026-04-04T00:00:00Z"
            self.acked = False

        def ack(self) -> None:
            self.acked = True

    class _FakeFuture:
        def __init__(self, callback):
            self._callback = callback

        def result(self) -> None:
            self._callback(_BadMessage())
            raise RuntimeError("stop stream")

        def cancel(self) -> None:
            return None

    class _FakeFlowControl:
        def __init__(self, max_messages: int):
            self.max_messages = max_messages

    class _FakeSubscriber:
        def subscribe(self, subscription_path: str, callback, flow_control):
            return _FakeFuture(callback)

        def close(self) -> None:
            return None

    class _FakePubSub:
        class types:
            FlowControl = _FakeFlowControl

        SubscriberClient = _FakeSubscriber

    triggers: list[bool] = []
    monkeypatch.setattr(streaming_pull, "ensure_pubsub_dependencies", lambda: None)
    monkeypatch.setattr(streaming_pull, "pubsub_v1", _FakePubSub)

    with pytest.raises(RuntimeError, match="stop stream"):
        streaming_pull.run_streaming_pull(
            subscription_path="projects/p/subscriptions/s",
            on_trigger=lambda: triggers.append(True) or True,
        )
    assert triggers == []


def test_run_streaming_pull_stops_when_trigger_fails_consecutively(monkeypatch, caplog) -> None:
    class _FakeMessage:
        def __init__(self, payload: dict):
            self.data = json.dumps(payload).encode("utf-8")
            self.message_id = "msg-1"
            self.publish_time = "2026-04-04T00:00:00Z"
            self.acked = False

        def ack(self) -> None:
            self.acked = True

    callback_invoked = False

    class _FakeFuture:
        def __init__(self, callback):
            self._callback = callback
            self.cancelled = False

        def result(self, timeout=None) -> None:
            nonlocal callback_invoked
            _ = timeout
            if not callback_invoked:
                callback_invoked = True
                self._callback(_FakeMessage({"emailAddress": "user@example.com", "historyId": "1"}))
            raise streaming_pull.FutureTimeoutError()

        def cancel(self) -> None:
            self.cancelled = True

    class _FakeFlowControl:
        def __init__(self, max_messages: int):
            self.max_messages = max_messages

    class _FakeSubscriber:
        last_future = None
        closed = False

        def __init__(self):
            self.future = None

        def subscribe(self, _subscription_path: str, callback, flow_control):
            _ = flow_control
            self.future = _FakeFuture(callback)
            _FakeSubscriber.last_future = self.future
            return self.future

        def close(self) -> None:
            _FakeSubscriber.closed = True

    class _FakePubSub:
        class types:
            FlowControl = _FakeFlowControl

        SubscriberClient = _FakeSubscriber

    monkeypatch.setattr(streaming_pull, "ensure_pubsub_dependencies", lambda: None)
    monkeypatch.setattr(streaming_pull, "pubsub_v1", _FakePubSub)

    try:
        streaming_pull.run_streaming_pull(
            subscription_path="projects/p/subscriptions/s",
            on_trigger=lambda: False,
            trigger_failure_max_consecutive=1,
        )
    except RuntimeError as exc:
        assert "Pub/Sub trigger worker failed" in str(exc)

    assert callback_invoked
    assert _FakeSubscriber.last_future is not None
    assert _FakeSubscriber.last_future.cancelled is True
    assert _FakeSubscriber.closed is True
    assert any("PUBSUB_WORKER_FATAL" in record.message for record in caplog.records)
