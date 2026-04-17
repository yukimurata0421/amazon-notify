from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

from .backoff import next_delay_seconds
from .config import LOGGER, save_state

try:
    import google.cloud.pubsub_v1 as pubsub_v1

    PUBSUB_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    PUBSUB_IMPORT_ERROR = exc

    def _raise_pubsub_import_error() -> NoReturn:
        if PUBSUB_IMPORT_ERROR is None:
            raise ModuleNotFoundError("google-cloud-pubsub is missing.")
        raise PUBSUB_IMPORT_ERROR

    class pubsub_v1:  # type: ignore[no-redef]
        class SubscriberClient:
            def __init__(self, *_args, **_kwargs):
                _raise_pubsub_import_error()

        class types:
            class FlowControl:
                def __init__(self, *_args, **_kwargs):
                    _raise_pubsub_import_error()


@dataclass(frozen=True)
class PubSubEvent:
    message_id: str
    publish_time: str
    history_id: int | None
    email_address: str | None


@dataclass(frozen=True)
class HeartbeatSnapshot:
    updated_at: float
    worker_last_seen_at: float | None
    callback_last_seen_at: float | None
    trigger_started_at: float | None
    trigger_completed_at: float | None
    last_trigger_ok: bool | None
    consecutive_trigger_failures: int
    last_error: str | None


def ensure_pubsub_dependencies() -> None:
    if PUBSUB_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "Pub/Sub dependencies are missing. Install with: `pip install .[pubsub]`"
        ) from PUBSUB_IMPORT_ERROR


def parse_pubsub_event(message) -> PubSubEvent:
    raw_text = message.data.decode("utf-8")
    payload = json.loads(raw_text)

    history_raw = payload.get("historyId")
    history_id: int | None = None
    if history_raw is not None:
        history_id = int(str(history_raw))

    email_address = payload.get("emailAddress")
    if email_address is not None:
        email_address = str(email_address)

    return PubSubEvent(
        message_id=str(getattr(message, "message_id", "(unknown)")),
        publish_time=str(getattr(message, "publish_time", "(unknown)")),
        history_id=history_id,
        email_address=email_address,
    )


def touch_heartbeat_file(heartbeat_file: Path) -> None:
    snapshot = HeartbeatSnapshot(
        updated_at=time.time(),
        worker_last_seen_at=None,
        callback_last_seen_at=None,
        trigger_started_at=None,
        trigger_completed_at=None,
        last_trigger_ok=None,
        consecutive_trigger_failures=0,
        last_error=None,
    )
    write_heartbeat_snapshot(heartbeat_file, snapshot)


def write_heartbeat_snapshot(heartbeat_file: Path, snapshot: HeartbeatSnapshot) -> None:
    payload = {
        "schema_version": 1,
        "updated_at": snapshot.updated_at,
        "worker_last_seen_at": snapshot.worker_last_seen_at,
        "callback_last_seen_at": snapshot.callback_last_seen_at,
        "trigger_started_at": snapshot.trigger_started_at,
        "trigger_completed_at": snapshot.trigger_completed_at,
        "last_trigger_ok": snapshot.last_trigger_ok,
        "consecutive_trigger_failures": snapshot.consecutive_trigger_failures,
        "last_error": snapshot.last_error,
    }
    # Heartbeat is a watchdog input, so avoid partial writes that can cause false negatives.
    # save_state() uses temp-file + replace to keep each snapshot atomic.
    save_state(heartbeat_file, payload)


def _validate_streaming_pull_args(
    *,
    pending_warn_threshold: int,
    queue_size: int | None,
    flow_control_max_messages: int,
    heartbeat_interval_seconds: float,
    trigger_failure_max_consecutive: int,
    trigger_failure_base_delay_seconds: float,
    trigger_failure_max_delay_seconds: float,
    idle_trigger_interval_seconds: float,
) -> int:
    # queue_size は過去名。pending backlog 警告しきい値の互換 alias としてのみ残す。
    if queue_size is not None:
        LOGGER.warning(
            "PUBSUB_QUEUE_SIZE_ALIAS_DEPRECATED: use pending_warn_threshold instead of queue_size"
        )
        pending_warn_threshold = queue_size
    if pending_warn_threshold < 1:
        raise ValueError("pending_warn_threshold must be >= 1")
    if flow_control_max_messages < 1:
        raise ValueError("flow_control_max_messages must be >= 1")
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat_interval_seconds must be > 0")
    if trigger_failure_max_consecutive < 1:
        raise ValueError("trigger_failure_max_consecutive must be >= 1")
    if trigger_failure_base_delay_seconds <= 0:
        raise ValueError("trigger_failure_base_delay_seconds must be > 0")
    if trigger_failure_max_delay_seconds <= 0:
        raise ValueError("trigger_failure_max_delay_seconds must be > 0")
    if idle_trigger_interval_seconds <= 0:
        raise ValueError("idle_trigger_interval_seconds must be > 0")
    return pending_warn_threshold


class _StreamingPullRunner:
    def __init__(
        self,
        *,
        subscription_path: str,
        on_trigger: Callable[[], bool],
        pending_warn_threshold: int,
        flow_control_max_messages: int,
        heartbeat_file: Path | None,
        heartbeat_interval_seconds: float,
        trigger_failure_max_consecutive: int,
        trigger_failure_base_delay_seconds: float,
        trigger_failure_max_delay_seconds: float,
        idle_trigger_interval_seconds: float,
    ) -> None:
        self.subscription_path = subscription_path
        self.on_trigger = on_trigger
        self.pending_warn_threshold = pending_warn_threshold
        self.flow_control_max_messages = flow_control_max_messages
        self.heartbeat_file = heartbeat_file
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.trigger_failure_max_consecutive = trigger_failure_max_consecutive
        self.trigger_failure_base_delay_seconds = trigger_failure_base_delay_seconds
        self.trigger_failure_max_delay_seconds = trigger_failure_max_delay_seconds
        self.idle_trigger_interval_seconds = idle_trigger_interval_seconds

        self.stop_event = threading.Event()
        self.trigger_event = threading.Event()
        self.pending_lock = threading.Lock()
        self.heartbeat_lock = threading.Lock()
        self.pending_event: PubSubEvent | None = None
        self.pending_count = 0
        self.backlog_warned = False
        self.worker_failure: Exception | None = None
        self.worker_failed = threading.Event()
        self.heartbeat_state = HeartbeatSnapshot(
            updated_at=time.time(),
            worker_last_seen_at=None,
            callback_last_seen_at=None,
            trigger_started_at=None,
            trigger_completed_at=None,
            last_trigger_ok=None,
            consecutive_trigger_failures=0,
            last_error=None,
        )

    def _update_heartbeat(
        self,
        *,
        worker_last_seen_at: float | None = None,
        callback_last_seen_at: float | None = None,
        trigger_started_at: float | None = None,
        trigger_completed_at: float | None = None,
        last_trigger_ok: bool | None = None,
        consecutive_trigger_failures: int | None = None,
        last_error: str | None = None,
        reset_last_error: bool = False,
    ) -> None:
        with self.heartbeat_lock:
            updated_last_error: str | None
            if reset_last_error:
                updated_last_error = None
            elif last_error is None:
                updated_last_error = self.heartbeat_state.last_error
            else:
                updated_last_error = last_error

            snapshot = replace(
                self.heartbeat_state,
                updated_at=time.time(),
                last_error=updated_last_error,
            )
            if worker_last_seen_at is not None:
                snapshot = replace(snapshot, worker_last_seen_at=worker_last_seen_at)
            if callback_last_seen_at is not None:
                snapshot = replace(
                    snapshot, callback_last_seen_at=callback_last_seen_at
                )
            if trigger_started_at is not None:
                snapshot = replace(snapshot, trigger_started_at=trigger_started_at)
            if trigger_completed_at is not None:
                snapshot = replace(snapshot, trigger_completed_at=trigger_completed_at)
            if last_trigger_ok is not None:
                snapshot = replace(snapshot, last_trigger_ok=last_trigger_ok)
            if consecutive_trigger_failures is not None:
                snapshot = replace(
                    snapshot,
                    consecutive_trigger_failures=consecutive_trigger_failures,
                )

            self.heartbeat_state = snapshot

    def _mark_heartbeat(self) -> None:
        if self.heartbeat_file is None:
            return
        try:
            with self.heartbeat_lock:
                snapshot = self.heartbeat_state
            write_heartbeat_snapshot(self.heartbeat_file, snapshot)
        except OSError as exc:
            LOGGER.warning(
                "PUBSUB_HEARTBEAT_UPDATE_FAILED: file=%s error=%s",
                self.heartbeat_file,
                exc,
            )

    def _queue_event(self, event: PubSubEvent) -> tuple[int, int | None]:
        with self.pending_lock:
            self.pending_count += 1
            if self.pending_event is None:
                self.pending_event = event
            elif event.history_id is None or self.pending_event.history_id is None:
                self.pending_event = event
            elif event.history_id >= self.pending_event.history_id:
                # Pub/Sub is a trigger path, not the durable processing frontier.
                # Catch-up correctness is anchored by Gmail state in run_once, so we can
                # collapse local backlog to the latest history_id trigger.
                self.pending_event = event

            if (
                self.pending_count >= self.pending_warn_threshold
                and not self.backlog_warned
            ):
                LOGGER.warning(
                    "PUBSUB_PENDING_BACKLOG_HIGH: pending=%s threshold=%s",
                    self.pending_count,
                    self.pending_warn_threshold,
                )
                self.backlog_warned = True

            collapsed = self.pending_count
            selected_history = (
                self.pending_event.history_id
                if self.pending_event is not None
                else None
            )
        self.trigger_event.set()
        return collapsed, selected_history

    def _dequeue_event(self) -> tuple[PubSubEvent | None, int]:
        with self.pending_lock:
            latest = self.pending_event
            collapsed = self.pending_count
            self.pending_event = None
            self.pending_count = 0
            self.backlog_warned = False
            self.trigger_event.clear()
        return latest, collapsed

    def _trigger_failure_delay(self, consecutive_failures: int) -> float:
        return next_delay_seconds(
            consecutive_failures,
            base_delay=self.trigger_failure_base_delay_seconds,
            max_delay=self.trigger_failure_max_delay_seconds,
            jitter_ratio=0.1,
        )

    def _run_trigger_once(
        self,
        *,
        start_log_event: str,
        done_log_event: str,
        start_log_args: tuple[Any, ...],
        consecutive_failures: int,
    ) -> tuple[int, bool]:
        started_at = time.time()
        self._update_heartbeat(trigger_started_at=started_at)
        LOGGER.info(start_log_event, *start_log_args)

        ok = False
        trigger_exc: Exception | None = None
        try:
            ok = self.on_trigger()
        except Exception as exc:
            trigger_exc = exc
            LOGGER.exception("%s_EXCEPTION: %s", done_log_event, exc)

        done_at = time.time()
        if ok:
            self._update_heartbeat(
                trigger_completed_at=done_at,
                last_trigger_ok=True,
                consecutive_trigger_failures=0,
                reset_last_error=True,
            )
            LOGGER.info("%s: ok=True", done_log_event)
            self._mark_heartbeat()
            return 0, True

        next_failures = consecutive_failures + 1
        reason = (
            str(trigger_exc) if trigger_exc is not None else "on_trigger returned False"
        )
        self._update_heartbeat(
            trigger_completed_at=done_at,
            last_trigger_ok=False,
            consecutive_trigger_failures=next_failures,
            last_error=reason,
        )
        LOGGER.warning(
            "%s: ok=False consecutive_failures=%s reason=%s",
            done_log_event,
            next_failures,
            reason,
        )
        if next_failures >= self.trigger_failure_max_consecutive:
            raise RuntimeError(
                "Pub/Sub trigger failed too many times consecutively "
                f"({next_failures}/{self.trigger_failure_max_consecutive})."
            )

        delay = self._trigger_failure_delay(next_failures)
        self._mark_heartbeat()
        if self.stop_event.wait(delay):
            return next_failures, False
        return next_failures, True

    def _worker_loop(self) -> None:
        last_history_id: int | None = None
        consecutive_failures = 0
        last_trigger_activity_at = time.time()

        try:
            while not self.stop_event.is_set():
                self._update_heartbeat(worker_last_seen_at=time.time())
                self._mark_heartbeat()
                if not self.trigger_event.wait(timeout=0.5):
                    now = time.time()
                    if (
                        now - last_trigger_activity_at
                        < self.idle_trigger_interval_seconds
                    ):
                        continue

                    consecutive_failures, should_continue = self._run_trigger_once(
                        start_log_event="PUBSUB_IDLE_TRIGGER_START: idle_for=%.1fs",
                        done_log_event="PUBSUB_IDLE_TRIGGER_DONE",
                        start_log_args=(now - last_trigger_activity_at,),
                        consecutive_failures=consecutive_failures,
                    )
                    last_trigger_activity_at = time.time()
                    if not should_continue:
                        break
                    continue

                while not self.stop_event.is_set():
                    latest, collapsed = self._dequeue_event()
                    if latest is None:
                        break

                    if (
                        latest.history_id is not None
                        and last_history_id is not None
                        and latest.history_id <= last_history_id
                    ):
                        # Same-or-older history_id means we already triggered from an
                        # equivalent or newer frontier, so this trigger can be skipped.
                        LOGGER.info(
                            "PUBSUB_TRIGGER_SKIPPED_DUPLICATE: history_id=%s last_history_id=%s collapsed=%s",
                            latest.history_id,
                            last_history_id,
                            collapsed,
                        )
                        continue

                    if latest.history_id is not None:
                        last_history_id = latest.history_id

                    consecutive_failures, should_continue = self._run_trigger_once(
                        start_log_event=(
                            "PUBSUB_TRIGGER_START: collapsed=%s history_id=%s email=%s"
                        ),
                        done_log_event="PUBSUB_TRIGGER_DONE",
                        start_log_args=(
                            collapsed,
                            latest.history_id,
                            latest.email_address,
                        ),
                        consecutive_failures=consecutive_failures,
                    )
                    last_trigger_activity_at = time.time()
                    if not should_continue:
                        break
        except Exception as exc:
            self.worker_failure = exc
            self.worker_failed.set()
            self.stop_event.set()
            self._update_heartbeat(last_error=str(exc))
            self._mark_heartbeat()
            LOGGER.exception("PUBSUB_WORKER_FATAL: %s", exc)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            self._mark_heartbeat()
            if self.stop_event.wait(self.heartbeat_interval_seconds):
                break

    def _callback(self, message: Any) -> None:
        if self.stop_event.is_set():
            message.ack()
            return

        try:
            event = parse_pubsub_event(message)
        except Exception as exc:
            LOGGER.error("PUBSUB_MESSAGE_PARSE_FAILED: %s", exc)
            self._update_heartbeat(
                callback_last_seen_at=time.time(), last_error=str(exc)
            )
            message.ack()
            return

        try:
            collapsed, selected_history = self._queue_event(event)
            LOGGER.info(
                "PUBSUB_MESSAGE_ACCEPTED: message_id=%s history_id=%s collapsed=%s selected_history=%s",
                event.message_id,
                event.history_id,
                collapsed,
                selected_history,
            )
        finally:
            self._update_heartbeat(callback_last_seen_at=time.time())
            self._mark_heartbeat()
            message.ack()

    def run(self) -> None:
        worker = threading.Thread(
            target=self._worker_loop, name="pubsub-trigger-worker", daemon=True
        )
        worker.start()

        heartbeat_worker: threading.Thread | None = None
        if self.heartbeat_file is not None:
            self._mark_heartbeat()
            heartbeat_worker = threading.Thread(
                target=self._heartbeat_loop,
                name="pubsub-heartbeat-worker",
                daemon=True,
            )
            heartbeat_worker.start()

        subscriber = pubsub_v1.SubscriberClient()
        flow_control = pubsub_v1.types.FlowControl(
            max_messages=self.flow_control_max_messages
        )
        stream_future = subscriber.subscribe(
            self.subscription_path,
            callback=self._callback,
            flow_control=flow_control,
        )
        LOGGER.info(
            "PUBSUB_STREAMING_PULL_START: subscription=%s", self.subscription_path
        )

        try:
            while not self.stop_event.is_set():
                if self.worker_failed.is_set():
                    raise RuntimeError(
                        "Pub/Sub trigger worker failed."
                    ) from self.worker_failure
                try:
                    stream_future.result(timeout=1.0)
                    break
                except TypeError:
                    stream_future.result()
                    break
                except FutureTimeoutError:
                    continue
        finally:
            self.stop_event.set()
            self.trigger_event.set()
            stream_future.cancel()
            worker.join(timeout=5)
            if heartbeat_worker is not None:
                heartbeat_worker.join(timeout=5)
            subscriber.close()


def run_streaming_pull(
    *,
    subscription_path: str,
    on_trigger: Callable[[], bool],
    pending_warn_threshold: int = 256,
    queue_size: int | None = None,
    flow_control_max_messages: int = 100,
    heartbeat_file: Path | None = None,
    heartbeat_interval_seconds: float = 30.0,
    trigger_failure_max_consecutive: int = 5,
    trigger_failure_base_delay_seconds: float = 1.0,
    trigger_failure_max_delay_seconds: float = 60.0,
    idle_trigger_interval_seconds: float = 300.0,
) -> None:
    normalized_pending_warn_threshold = _validate_streaming_pull_args(
        pending_warn_threshold=pending_warn_threshold,
        queue_size=queue_size,
        flow_control_max_messages=flow_control_max_messages,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        trigger_failure_max_consecutive=trigger_failure_max_consecutive,
        trigger_failure_base_delay_seconds=trigger_failure_base_delay_seconds,
        trigger_failure_max_delay_seconds=trigger_failure_max_delay_seconds,
        idle_trigger_interval_seconds=idle_trigger_interval_seconds,
    )
    ensure_pubsub_dependencies()
    runner = _StreamingPullRunner(
        subscription_path=subscription_path,
        on_trigger=on_trigger,
        pending_warn_threshold=normalized_pending_warn_threshold,
        flow_control_max_messages=flow_control_max_messages,
        heartbeat_file=heartbeat_file,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        trigger_failure_max_consecutive=trigger_failure_max_consecutive,
        trigger_failure_base_delay_seconds=trigger_failure_base_delay_seconds,
        trigger_failure_max_delay_seconds=trigger_failure_max_delay_seconds,
        idle_trigger_interval_seconds=idle_trigger_interval_seconds,
    )
    runner.run()
