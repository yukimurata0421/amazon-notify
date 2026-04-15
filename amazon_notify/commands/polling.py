from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .. import config as app_config
from ..runtime import MIN_POLL_INTERVAL_SECONDS, RuntimeConfig


def resolve_watchdog_options(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
) -> tuple[Path, float, float, str]:
    heartbeat_file = (
        app_config.resolve_runtime_path(args.heartbeat_file)
        if args.heartbeat_file
        else runtime.pubsub.heartbeat_file
    )
    heartbeat_interval_seconds = (
        float(args.heartbeat_interval_seconds)
        if args.heartbeat_interval_seconds is not None
        else float(runtime.pubsub.heartbeat_interval_seconds)
    )
    heartbeat_max_age_seconds = (
        float(args.heartbeat_max_age_seconds)
        if args.heartbeat_max_age_seconds is not None
        else float(runtime.pubsub.heartbeat_max_age_seconds)
    )
    main_service_name = (
        args.main_service_name.strip()
        if args.main_service_name
        else str(runtime.pubsub.main_service_name)
    )
    return (
        heartbeat_file,
        heartbeat_interval_seconds,
        heartbeat_max_age_seconds,
        main_service_name,
    )


def validate_watchdog_options(
    heartbeat_interval_seconds: float,
    heartbeat_max_age_seconds: float,
    main_service_name: str,
    *,
    stderr_error: Callable[[str], None],
) -> None:
    if heartbeat_interval_seconds <= 0:
        stderr_error("heartbeat-interval-seconds は 0 より大きい値を指定してください。")
        sys.exit(1)
    if heartbeat_max_age_seconds <= 0:
        stderr_error("heartbeat-max-age-seconds は 0 より大きい値を指定してください。")
        sys.exit(1)
    if not main_service_name:
        stderr_error("main-service-name は空文字にできません。")
        sys.exit(1)


def should_run_fallback_polling(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
    heartbeat_file: Path,
    heartbeat_max_age_seconds: float,
    main_service_name: str,
    *,
    evaluate_failover_watchdog_fn: Callable[..., bool],
    stderr_error: Callable[[str], None],
) -> bool:
    if not args.fallback_watchdog:
        return True
    if not args.once:
        stderr_error("--fallback-watchdog は --once と併用してください。")
        sys.exit(1)
    return evaluate_failover_watchdog_fn(
        state_file=runtime.state_file,
        discord_webhook_url=runtime.discord_webhook_url,
        service_name=main_service_name,
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=heartbeat_max_age_seconds,
        dry_run=runtime.dry_run,
    )


def run_polling_mode(
    args: argparse.Namespace,
    config: dict,
    runtime: RuntimeConfig,
    *,
    run_once_with_guard_fn: Callable[[RuntimeConfig], bool],
    sleep_fn: Callable[[float], None] = time.sleep,
    stderr_error: Callable[[str], None],
) -> None:
    poll_interval = args.interval or int(config.get("poll_interval_seconds", 60))
    if poll_interval < MIN_POLL_INTERVAL_SECONDS:
        stderr_error(
            f"interval は {MIN_POLL_INTERVAL_SECONDS} 以上を指定してください。"
        )
        sys.exit(1)

    first_run_ok = run_once_with_guard_fn(runtime)
    if args.once:
        if not first_run_ok:
            sys.exit(1)
        return

    app_config.LOGGER.info(
        "LOOP_MODE_START: interval=%ss dry_run=%s",
        poll_interval,
        runtime.dry_run,
    )
    while True:
        sleep_fn(poll_interval)
        run_once_with_guard_fn(runtime)
