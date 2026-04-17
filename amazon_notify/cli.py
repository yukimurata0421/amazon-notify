from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import config as app_config
from .commands import arguments as arguments_command
from .commands import dispatch as dispatch_command
from .commands import health as health_command
from .commands import polling as polling_command
from .commands import reauth as reauth_command
from .commands import scenario as scenario_command
from .commands import streaming as streaming_command
from .commands import watch as watch_command
from .config import RuntimePaths
from .discord_client import send_discord_test
from .failover import evaluate_failover_watchdog
from .gmail_client import (
    get_gmail_service_with_status,
    run_oauth_flow,
    start_gmail_watch_with_retry,
)
from .notifier import report_unhandled_exception, run_once
from .runtime import RuntimeConfig, looks_like_discord_webhook_url, mask_webhook_url
from .runtime import build_runtime as build_runtime_impl
from .runtime import compile_optional_pattern as compile_optional_pattern_impl
from .runtime import validate_config as validate_config_impl
from .streaming_pull import run_streaming_pull


def compile_optional_pattern(pattern: str | None, config_key: str):
    try:
        return compile_optional_pattern_impl(pattern, config_key)
    except ValueError as exc:
        _stderr_error(str(exc))
        sys.exit(1)


def _stderr_error(message: str) -> None:
    sys.stderr.write(f"[ERROR] {message}\n")


def load_config_or_exit(paths: RuntimePaths) -> dict[str, Any]:
    if not paths.config.exists():
        app_config.LOGGER.error("CONFIG_MISSING: %s", paths.config)
        _stderr_error(f"{paths.config} が見つかりません。")
        sys.exit(1)

    try:
        return app_config.load_config(paths.config)
    except json.JSONDecodeError as exc:
        app_config.LOGGER.error("CONFIG_JSON_INVALID: %s", exc)
        _stderr_error(f"config.json の JSON が不正です: {exc}")
        sys.exit(1)
    except OSError as exc:
        app_config.LOGGER.error("CONFIG_READ_FAILED: %s", exc)
        _stderr_error(f"config.json を読み込めませんでした: {exc}")
        sys.exit(1)


def load_config_for_health_check(
    paths: RuntimePaths,
) -> tuple[dict[str, Any] | None, list[str]]:
    return health_command.load_config_for_health_check(
        paths,
        validate_config=lambda config: validate_config_impl(
            config,
            paths=paths,
        ),
    )


def run_health_check(
    paths: RuntimePaths,
    config: dict[str, Any] | None,
    validation_errors: list[str],
) -> int:
    exit_code, report = health_command.run_health_check(
        paths,
        config=config,
        validation_errors=validation_errors,
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return exit_code


def print_validation_errors(errors: list[str]) -> None:
    for err in errors:
        _stderr_error(err)


def run_once_with_guard(runtime: RuntimeConfig) -> bool:
    try:
        run_once(runtime)
        return True
    except Exception as exc:
        app_config.LOGGER.exception("RUN_ONCE_UNHANDLED_EXCEPTION: %s", exc)
        try:
            report_unhandled_exception(runtime, exc)
        except Exception as report_exc:
            app_config.LOGGER.exception(
                "UNHANDLED_EXCEPTION_REPORT_FAILED: %s",
                report_exc,
            )
        return False


def handle_reauth(args: argparse.Namespace, paths: RuntimePaths) -> bool:
    return reauth_command.handle_reauth(
        args,
        paths=paths,
        run_oauth_flow_fn=run_oauth_flow,
    )


def handle_health_check(args: argparse.Namespace, paths: RuntimePaths) -> bool:
    if not args.health_check:
        return False
    health_config, health_validation_errors = load_config_for_health_check(paths)
    sys.exit(run_health_check(paths, health_config, health_validation_errors))


def handle_validate_config(
    args: argparse.Namespace, validation_errors: list[str]
) -> bool:
    if not args.validate_config:
        return False
    if validation_errors:
        print_validation_errors(validation_errors)
        sys.exit(1)
    sys.stdout.write("[OK] config.json の検証に成功しました。\n")
    return True


def handle_test_discord(
    args: argparse.Namespace, config: dict[str, Any], runtime: RuntimeConfig
) -> bool:
    if not args.test_discord:
        return False
    webhook_url = config["discord_webhook_url"]
    sent = send_discord_test(
        webhook_url,
        "Amazon Notify の test-discord コマンドから送信しました。",
        dedupe_state_path=runtime.discord_dedupe_state_file,
    )
    if not sent:
        app_config.LOGGER.error("TEST_DISCORD_FAILED")
        _stderr_error("Discord テスト通知の送信に失敗しました。")
        sys.exit(1)
    app_config.LOGGER.info("TEST_DISCORD_SUCCESS")
    sys.stdout.write("[OK] Discord テスト通知を送信しました。\n")
    return True


def handle_setup_watch(
    args: argparse.Namespace, config: dict[str, Any], *, paths: RuntimePaths
) -> None:
    watch_command.handle_setup_watch(
        args,
        config,
        paths=paths,
        stderr_error=_stderr_error,
        load_state_fn=app_config.load_state,
        get_gmail_service_with_status_fn=get_gmail_service_with_status,
        start_gmail_watch_with_retry_fn=start_gmail_watch_with_retry,
    )


def resolve_watchdog_options(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
) -> tuple[Path, float, float, str]:
    return polling_command.resolve_watchdog_options(args, runtime)


def validate_watchdog_options(
    heartbeat_interval_seconds: float,
    heartbeat_max_age_seconds: float,
    main_service_name: str,
) -> None:
    polling_command.validate_watchdog_options(
        heartbeat_interval_seconds,
        heartbeat_max_age_seconds,
        main_service_name,
        stderr_error=_stderr_error,
    )


def handle_streaming_mode(
    args: argparse.Namespace,
    config: dict[str, Any],
    runtime: RuntimeConfig,
    heartbeat_file: Path,
    heartbeat_interval_seconds: float,
) -> None:
    streaming_command.handle_streaming_mode(
        args,
        config,
        runtime,
        heartbeat_file,
        heartbeat_interval_seconds,
        run_once_with_guard_fn=run_once_with_guard,
        run_streaming_pull_fn=run_streaming_pull,
        sleep_fn=time.sleep,
        stderr_error=_stderr_error,
    )


def should_run_fallback_polling(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
    heartbeat_file: Path,
    heartbeat_max_age_seconds: float,
    main_service_name: str,
) -> bool:
    return polling_command.should_run_fallback_polling(
        args,
        runtime,
        heartbeat_file,
        heartbeat_max_age_seconds,
        main_service_name,
        evaluate_failover_watchdog_fn=evaluate_failover_watchdog,
        stderr_error=_stderr_error,
    )


def run_polling_mode(
    args: argparse.Namespace, config: dict[str, Any], runtime: RuntimeConfig
) -> None:
    polling_command.run_polling_mode(
        args,
        config,
        runtime,
        run_once_with_guard_fn=run_once_with_guard,
        sleep_fn=time.sleep,
        stderr_error=_stderr_error,
    )


def main() -> None:
    parser = arguments_command.build_parser()
    args = parser.parse_args()
    arguments_command.validate_action_conflicts(args)
    paths = app_config.get_runtime_paths(args.config)

    if handle_reauth(args, paths):
        return

    if handle_health_check(args, paths):
        return

    config = load_config_or_exit(paths)
    validation_errors = validate_config_impl(config, paths=paths)

    if handle_validate_config(args, validation_errors):
        return

    log_path = (
        app_config.resolve_runtime_path(args.log_file, base_dir=paths.runtime_dir)
        if args.log_file
        else app_config.resolve_runtime_path(
            config.get("log_file", str(paths.default_log)), base_dir=paths.runtime_dir
        )
    )
    app_config.setup_logging(
        log_path, structured=bool(config.get("structured_logging", False))
    )

    if validation_errors:
        app_config.LOGGER.error("CONFIG_INVALID: %s", " | ".join(validation_errors))
        print_validation_errors(validation_errors)
        sys.exit(1)

    if not looks_like_discord_webhook_url(config["discord_webhook_url"]):
        app_config.LOGGER.warning(
            "CONFIG_DISCORD_WEBHOOK_URL_UNUSUAL: value=%s",
            mask_webhook_url(config["discord_webhook_url"]),
        )

    runtime = build_runtime_impl(
        config,
        paths=paths,
        dry_run=args.dry_run,
    )

    if handle_test_discord(args, config, runtime):
        return

    if args.setup_watch:
        handle_setup_watch(args, config, paths=paths)
        return

    if dispatch_command.handle_rebuild_indexes(args, runtime):
        return
    if dispatch_command.handle_status_report(args, runtime):
        return
    if dispatch_command.handle_doctor_report(args, runtime):
        return
    if dispatch_command.handle_verify_state_report(args, runtime):
        return
    if dispatch_command.handle_metrics_report(args, runtime):
        return
    if scenario_command.handle_scenario_harness(args, runtime):
        return
    (
        heartbeat_file,
        heartbeat_interval_seconds,
        heartbeat_max_age_seconds,
        main_service_name,
    ) = resolve_watchdog_options(args, runtime)
    validate_watchdog_options(
        heartbeat_interval_seconds,
        heartbeat_max_age_seconds,
        main_service_name,
    )

    if args.streaming_pull:
        handle_streaming_mode(
            args,
            config,
            runtime,
            heartbeat_file,
            heartbeat_interval_seconds,
        )
        return

    if not should_run_fallback_polling(
        args,
        runtime,
        heartbeat_file,
        heartbeat_max_age_seconds,
        main_service_name,
    ):
        return

    run_polling_mode(args, config, runtime)


if __name__ == "__main__":
    main()
