from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .. import config as app_config
from ..backoff import next_delay_seconds
from ..runtime import RuntimeConfig
from ..streaming_pull import run_streaming_pull


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
    run_once_with_guard_fn(runtime)

    reconnect_attempt = 0
    reconnect_max_attempts = runtime.pubsub_stream_reconnect_max_attempts
    reconnect_base_delay = runtime.pubsub_stream_reconnect_base_delay_seconds
    reconnect_max_delay = runtime.pubsub_stream_reconnect_max_delay_seconds

    while True:
        try:

            def _trigger(_runtime: RuntimeConfig = runtime) -> bool:
                return run_once_with_guard_fn(_runtime)

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
            )
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            reconnect_attempt += 1
            app_config.LOGGER.exception(
                "STREAMING_PULL_SESSION_FAILED: attempt=%s error=%s",
                reconnect_attempt,
                exc,
            )
            if (
                reconnect_max_attempts > 0
                and reconnect_attempt >= reconnect_max_attempts
            ):
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
