from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import config as app_config
from ..backoff import next_delay_seconds
from ..failover import load_heartbeat_snapshot
from ..runtime import RuntimeConfig
from ..service_status import ServiceStatusTracker
from ..streaming_pull import run_streaming_pull


def _should_mark_progress_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    previous_updated_at: float | None,
    now_ts: float,
    max_age_seconds: float,
) -> tuple[bool, float | None]:
    if snapshot is None:
        return False, previous_updated_at

    updated_at_raw = snapshot.get("updated_at")
    if not isinstance(updated_at_raw, (int, float)):
        return False, previous_updated_at
    updated_at = float(updated_at_raw)
    if now_ts - updated_at > max_age_seconds:
        return False, previous_updated_at

    worker_last_seen_at_raw = snapshot.get("worker_last_seen_at")
    if isinstance(worker_last_seen_at_raw, (int, float)):
        worker_last_seen_at = float(worker_last_seen_at_raw)
        if now_ts - worker_last_seen_at > max_age_seconds:
            return False, previous_updated_at

    if previous_updated_at is not None and updated_at <= previous_updated_at:
        return False, previous_updated_at

    return True, updated_at


def handle_streaming_mode(
    args: argparse.Namespace,
    config: dict,
    runtime: RuntimeConfig,
    heartbeat_file: Path,
    heartbeat_interval_seconds: float,
    *,
    run_once_with_guard_fn: Callable[[RuntimeConfig], bool],
    run_streaming_pull_fn: Callable[..., None] = run_streaming_pull,
    sleep_fn: Callable[[float], None] = time.sleep,
    stderr_error: Callable[[str], None],
) -> None:
    if args.fallback_watchdog:
        stderr_error("--streaming-pull と --fallback-watchdog は同時に指定できません。")
        sys.exit(1)
    if args.once:
        stderr_error("--streaming-pull と --once は同時に指定できません。")
        sys.exit(1)
    if args.interval is not None:
        stderr_error("--streaming-pull と --interval は同時に指定できません。")
        sys.exit(1)

    subscription = (
        args.pubsub_subscription or config.get("pubsub_subscription", "")
    ).strip()
    if not subscription:
        stderr_error("StreamingPull には pubsub subscription が必要です。")
        sys.exit(1)

    app_config.LOGGER.info("STREAMING_PULL_MODE_START: subscription=%s", subscription)
    status_tracker = ServiceStatusTracker(
        status_file=runtime.service_status_file,
        heartbeat_file=heartbeat_file,
    )
    bootstrap_ok = run_once_with_guard_fn(runtime)
    status_tracker.mark_trigger_result(
        bootstrap_ok,
        reason="bootstrap_run_once_failed",
    )

    reconnect_attempt = 0
    reconnect_max_attempts = runtime.pubsub_stream_reconnect_max_attempts
    reconnect_base_delay = runtime.pubsub_stream_reconnect_base_delay_seconds
    reconnect_max_delay = runtime.pubsub_stream_reconnect_max_delay_seconds
    heartbeat_stop = threading.Event()
    progress_heartbeat_max_age = float(runtime.pubsub_heartbeat_max_age_seconds)

    def _status_heartbeat_loop() -> None:
        last_seen_updated_at: float | None = None
        while not heartbeat_stop.wait(heartbeat_interval_seconds):
            now_ts = time.time()
            snapshot = load_heartbeat_snapshot(heartbeat_file)
            should_mark, last_seen_updated_at = _should_mark_progress_from_snapshot(
                snapshot,
                previous_updated_at=last_seen_updated_at,
                now_ts=now_ts,
                max_age_seconds=progress_heartbeat_max_age,
            )
            if should_mark:
                status_tracker.mark_system_progress(reason="control_plane_progress")

    heartbeat_thread = threading.Thread(
        target=_status_heartbeat_loop,
        name="service-status-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    try:
        while True:
            try:

                def _trigger(_runtime: RuntimeConfig = runtime) -> bool:
                    ok = run_once_with_guard_fn(_runtime)
                    status_tracker.mark_trigger_result(
                        ok,
                        reason="run_once_failed",
                    )
                    return ok

                run_streaming_pull_fn(
                    subscription_path=subscription,
                    on_trigger=_trigger,
                    pending_warn_threshold=args.pubsub_pending_warn_threshold,
                    flow_control_max_messages=args.pubsub_flow_max_messages,
                    heartbeat_file=heartbeat_file,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                    trigger_failure_max_consecutive=runtime.pubsub_trigger_failure_max_consecutive,
                    trigger_failure_base_delay_seconds=runtime.pubsub_trigger_failure_base_delay_seconds,
                    trigger_failure_max_delay_seconds=runtime.pubsub_trigger_failure_max_delay_seconds,
                    idle_trigger_interval_seconds=runtime.pubsub_idle_trigger_interval_seconds,
                )
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                reconnect_attempt += 1
                recovery_action = (
                    "stream_recycle" if reconnect_attempt == 1 else "client_recreate"
                )
                status_tracker.mark_reconnect_attempt(
                    action=recovery_action,
                    reason="pubsub_stream_session_failed",
                )
                app_config.LOGGER.exception(
                    "STREAMING_PULL_SESSION_FAILED: attempt=%s error=%s",
                    reconnect_attempt,
                    exc,
                )
                if (
                    reconnect_max_attempts > 0
                    and reconnect_attempt >= reconnect_max_attempts
                ):
                    status_tracker.mark_failed(
                        reason="pubsub_circuit_breaker_open",
                    )
                    stderr_error(
                        "StreamingPull の再接続試行回数が上限に達しました。"
                        f" attempts={reconnect_attempt}"
                    )
                    sys.exit(1)

                delay = next_delay_seconds(
                    reconnect_attempt,
                    base_delay=reconnect_base_delay,
                    max_delay=reconnect_max_delay,
                    jitter_ratio=0.1,
                )
                app_config.LOGGER.warning(
                    "STREAMING_PULL_RECONNECT_RETRY: attempt=%s wait=%.2fs",
                    reconnect_attempt,
                    delay,
                )
                sleep_fn(delay)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=heartbeat_interval_seconds + 1.0)
