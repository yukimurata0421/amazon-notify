from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

from .backoff import next_delay_seconds
from .config import LOGGER

try:
    from google.cloud import pubsub_v1
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
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
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
    heartbeat_file.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.utime(heartbeat_file, (now, now))


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
) -> None:
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

    ensure_pubsub_dependencies()

    stop_event = threading.Event()
    trigger_event = threading.Event()
    pending_lock = threading.Lock()
    heartbeat_lock = threading.Lock()
    pending_event: PubSubEvent | None = None
    pending_count = 0
    backlog_warned = False
    worker_failure: Exception | None = None
    worker_failed = threading.Event()

    heartbeat_state = HeartbeatSnapshot(
        updated_at=time.time(),
        worker_last_seen_at=None,
        callback_last_seen_at=None,
        trigger_started_at=None,
        trigger_completed_at=None,
        last_trigger_ok=None,
        consecutive_trigger_failures=0,
        last_error=None,
    )

    def update_heartbeat(
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
        nonlocal heartbeat_state
        with heartbeat_lock:
            updated_last_error: str | None
            if reset_last_error:
                updated_last_error = None
            elif last_error is None:
                updated_last_error = heartbeat_state.last_error
            else:
                updated_last_error = last_error

            heartbeat_state = HeartbeatSnapshot(
                updated_at=time.time(),
                worker_last_seen_at=(
                    heartbeat_state.worker_last_seen_at
                    if worker_last_seen_at is None
                    else worker_last_seen_at
                ),
                callback_last_seen_at=(
                    heartbeat_state.callback_last_seen_at
                    if callback_last_seen_at is None
                    else callback_last_seen_at
                ),
                trigger_started_at=(
                    heartbeat_state.trigger_started_at
                    if trigger_started_at is None
                    else trigger_started_at
                ),
                trigger_completed_at=(
                    heartbeat_state.trigger_completed_at
                    if trigger_completed_at is None
                    else trigger_completed_at
                ),
                last_trigger_ok=(
                    heartbeat_state.last_trigger_ok
                    if last_trigger_ok is None
                    else last_trigger_ok
                ),
                consecutive_trigger_failures=(
                    heartbeat_state.consecutive_trigger_failures
                    if consecutive_trigger_failures is None
                    else consecutive_trigger_failures
                ),
                last_error=updated_last_error,
            )

    def mark_heartbeat() -> None:
        if heartbeat_file is None:
            return
        try:
            with heartbeat_lock:
                snapshot = heartbeat_state
            write_heartbeat_snapshot(heartbeat_file, snapshot)
        except OSError as exc:
            LOGGER.warning("PUBSUB_HEARTBEAT_UPDATE_FAILED: file=%s error=%s", heartbeat_file, exc)

    def queue_event(event: PubSubEvent) -> tuple[int, int | None]:
        nonlocal backlog_warned, pending_event, pending_count
        with pending_lock:
            pending_count += 1
            if pending_event is None:
                pending_event = event
            elif event.history_id is None or pending_event.history_id is None:
                pending_event = event
            elif event.history_id >= pending_event.history_id:
                pending_event = event

            if pending_count >= pending_warn_threshold and not backlog_warned:
                LOGGER.warning(
                    "PUBSUB_PENDING_BACKLOG_HIGH: pending=%s threshold=%s",
                    pending_count,
                    pending_warn_threshold,
                )
                backlog_warned = True

            collapsed = pending_count
            selected_history = pending_event.history_id if pending_event is not None else None
        trigger_event.set()
        return collapsed, selected_history

    def dequeue_event() -> tuple[PubSubEvent | None, int]:
        nonlocal backlog_warned, pending_event, pending_count
        with pending_lock:
            latest = pending_event
            collapsed = pending_count
            pending_event = None
            pending_count = 0
            backlog_warned = False
            trigger_event.clear()
        return latest, collapsed

    def worker_loop() -> None:
        nonlocal worker_failure
        last_history_id: int | None = None
        consecutive_failures = 0

        try:
            while not stop_event.is_set():
                update_heartbeat(worker_last_seen_at=time.time())
                mark_heartbeat()
                if not trigger_event.wait(timeout=0.5):
                    continue

                while not stop_event.is_set():
                    latest, collapsed = dequeue_event()
                    if latest is None:
                        break

                    if (
                        latest.history_id is not None
                        and last_history_id is not None
                        and latest.history_id <= last_history_id
                    ):
                        LOGGER.info(
                            "PUBSUB_TRIGGER_SKIPPED_DUPLICATE: history_id=%s last_history_id=%s collapsed=%s",
                            latest.history_id,
                            last_history_id,
                            collapsed,
                        )
                        continue

                    if latest.history_id is not None:
                        last_history_id = latest.history_id

                    started_at = time.time()
                    update_heartbeat(trigger_started_at=started_at)
                    LOGGER.info(
                        "PUBSUB_TRIGGER_START: collapsed=%s history_id=%s email=%s",
                        collapsed,
                        latest.history_id,
                        latest.email_address,
                    )

                    ok = False
                    trigger_exc: Exception | None = None
                    try:
                        ok = on_trigger()
                    except Exception as exc:
                        trigger_exc = exc
                        LOGGER.exception("PUBSUB_TRIGGER_EXCEPTION: %s", exc)

                    done_at = time.time()
                    if ok:
                        consecutive_failures = 0
                        update_heartbeat(
                            trigger_completed_at=done_at,
                            last_trigger_ok=True,
                            consecutive_trigger_failures=0,
                            reset_last_error=True,
                        )
                        LOGGER.info("PUBSUB_TRIGGER_DONE: ok=True")
                    else:
                        consecutive_failures += 1
                        reason = str(trigger_exc) if trigger_exc is not None else "on_trigger returned False"
                        update_heartbeat(
                            trigger_completed_at=done_at,
                            last_trigger_ok=False,
                            consecutive_trigger_failures=consecutive_failures,
                            last_error=reason,
                        )
                        LOGGER.warning(
                            "PUBSUB_TRIGGER_DONE: ok=False consecutive_failures=%s reason=%s",
                            consecutive_failures,
                            reason,
                        )
                        if consecutive_failures >= trigger_failure_max_consecutive:
                            raise RuntimeError(
                                "Pub/Sub trigger failed too many times consecutively "
                                f"({consecutive_failures}/{trigger_failure_max_consecutive})."
                            )

                        delay = next_delay_seconds(
                            consecutive_failures,
                            base_delay=trigger_failure_base_delay_seconds,
                            max_delay=trigger_failure_max_delay_seconds,
                        )
                        if stop_event.wait(delay):
                            break

                    mark_heartbeat()
        except Exception as exc:
            worker_failure = exc
            worker_failed.set()
            stop_event.set()
            update_heartbeat(last_error=str(exc))
            mark_heartbeat()
            LOGGER.exception("PUBSUB_WORKER_FATAL: %s", exc)

    worker = threading.Thread(target=worker_loop, name="pubsub-trigger-worker", daemon=True)
    worker.start()

    heartbeat_worker: threading.Thread | None = None
    if heartbeat_file is not None:
        mark_heartbeat()

        def heartbeat_loop() -> None:
            while not stop_event.is_set():
                mark_heartbeat()
                if stop_event.wait(heartbeat_interval_seconds):
                    break

        heartbeat_worker = threading.Thread(
            target=heartbeat_loop,
            name="pubsub-heartbeat-worker",
            daemon=True,
        )
        heartbeat_worker.start()

    subscriber = pubsub_v1.SubscriberClient()
    flow_control = pubsub_v1.types.FlowControl(max_messages=flow_control_max_messages)

    def callback(message) -> None:
        if stop_event.is_set():
            message.ack()
            return

        try:
            event = parse_pubsub_event(message)
        except Exception as exc:
            LOGGER.error("PUBSUB_MESSAGE_PARSE_FAILED: %s", exc)
            update_heartbeat(callback_last_seen_at=time.time(), last_error=str(exc))
            message.ack()
            return

        try:
            collapsed, selected_history = queue_event(event)
            LOGGER.info(
                "PUBSUB_MESSAGE_ACCEPTED: message_id=%s history_id=%s collapsed=%s selected_history=%s",
                event.message_id,
                event.history_id,
                collapsed,
                selected_history,
            )
        finally:
            update_heartbeat(callback_last_seen_at=time.time())
            mark_heartbeat()
            message.ack()

    stream_future = subscriber.subscribe(
        subscription_path,
        callback=callback,
        flow_control=flow_control,
    )
    LOGGER.info("PUBSUB_STREAMING_PULL_START: subscription=%s", subscription_path)

    try:
        while not stop_event.is_set():
            if worker_failed.is_set():
                raise RuntimeError("Pub/Sub trigger worker failed.") from worker_failure
            try:
                stream_future.result(timeout=1.0)
                break
            except TypeError:
                stream_future.result()
                break
            except FutureTimeoutError:
                continue
    finally:
        stop_event.set()
        trigger_event.set()
        stream_future.cancel()
        worker.join(timeout=5)
        if heartbeat_worker is not None:
            heartbeat_worker.join(timeout=5)
        subscriber.close()
