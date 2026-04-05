from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config as app_config
from .commands import health as health_command
from .commands import polling as polling_command
from .commands import reauth as reauth_command
from .commands import streaming as streaming_command
from .commands import watch as watch_command
from .discord_client import send_discord_alert, send_discord_test
from .failover import evaluate_failover_watchdog
from .gmail_client import (
    get_gmail_service_with_status,
    run_oauth_flow,
    start_gmail_watch_with_retry,
)
from .notifier import run_once
from .runtime import RuntimeConfig, looks_like_discord_webhook_url
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


def build_runtime(config: dict, dry_run: bool = False) -> RuntimeConfig:
    return build_runtime_impl(config, paths=app_config.get_runtime_paths(), dry_run=dry_run)


def _stderr_error(message: str) -> None:
    sys.stderr.write(f"[ERROR] {message}\n")


def load_config_or_exit() -> dict:
    if not app_config.CONFIG_PATH.exists():
        app_config.LOGGER.error("CONFIG_MISSING: %s", app_config.CONFIG_PATH)
        _stderr_error(f"{app_config.CONFIG_PATH} が見つかりません。")
        sys.exit(1)

    try:
        return app_config.load_config(app_config.CONFIG_PATH)
    except json.JSONDecodeError as exc:
        app_config.LOGGER.error("CONFIG_JSON_INVALID: %s", exc)
        _stderr_error(f"config.json の JSON が不正です: {exc}")
        sys.exit(1)
    except OSError as exc:
        app_config.LOGGER.error("CONFIG_READ_FAILED: %s", exc)
        _stderr_error(f"config.json を読み込めませんでした: {exc}")
        sys.exit(1)


def load_config_for_health_check() -> tuple[dict | None, list[str]]:
    return health_command.load_config_for_health_check(validate_config=validate_config)


def validate_config(config: dict) -> list[str]:
    return validate_config_impl(config, paths=app_config.get_runtime_paths())


def run_health_check(config: dict | None, validation_errors: list[str]) -> int:
    exit_code, report = health_command.run_health_check(
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
        if runtime.discord_webhook_url and not runtime.dry_run:
            send_discord_alert(
                runtime.discord_webhook_url,
                f"未処理例外を検知しました。次周期で再試行します。\nエラー: {exc}",
            )
        return False


def handle_reauth(args: argparse.Namespace) -> bool:
    return reauth_command.handle_reauth(
        args,
        run_oauth_flow_fn=run_oauth_flow,
    )


def handle_health_check(args: argparse.Namespace) -> bool:
    if not args.health_check:
        return False
    health_config, health_validation_errors = load_config_for_health_check()
    sys.exit(run_health_check(health_config, health_validation_errors))


def handle_validate_config(args: argparse.Namespace, validation_errors: list[str]) -> bool:
    if not args.validate_config:
        return False
    if validation_errors:
        print_validation_errors(validation_errors)
        sys.exit(1)
    sys.stdout.write("[OK] config.json の検証に成功しました。\n")
    return True


def handle_setup_watch(args: argparse.Namespace, config: dict) -> None:
    watch_command.handle_setup_watch(
        args,
        config,
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
    config: dict,
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


def run_polling_mode(args: argparse.Namespace, config: dict, runtime: RuntimeConfig) -> None:
    polling_command.run_polling_mode(
        args,
        config,
        runtime,
        run_once_with_guard_fn=run_once_with_guard,
        sleep_fn=time.sleep,
        stderr_error=_stderr_error,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Amazon配送メールを監視してDiscordに通知")
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="設定ファイルの保存先。相対パスはカレントディレクトリ基準。",
    )
    parser.add_argument("--once", action="store_true", help="1回だけ実行して終了する（デフォルトはループ実行）")
    parser.add_argument("--interval", type=int, help="ループ実行時の待ち時間（秒）。configのpoll_interval_secondsより優先される。")
    parser.add_argument("--reauth", action="store_true", help="対話OAuthで token.json を再作成して終了する。")
    parser.add_argument("--dry-run", action="store_true", help="Gmail取得と判定のみ実行し、Discord送信とstate更新を行わない。")
    parser.add_argument("--test-discord", action="store_true", help="設定済み Discord Webhook へテスト通知を送信して終了する。")
    parser.add_argument("--validate-config", action="store_true", help="設定ファイルを検証して終了する。")
    parser.add_argument("--health-check", action="store_true", help="実行前提のヘルスチェック結果をJSONで出力して終了する。")
    parser.add_argument("--log-file", type=str, help="ログファイルの保存先（未指定時は logs/amazon_mail_notifier.log）。")
    parser.add_argument("--streaming-pull", action="store_true", help="Pub/Sub StreamingPull でイベント駆動実行する。")
    parser.add_argument(
        "--pubsub-subscription",
        type=str,
        help="StreamingPull 対象の subscription（projects/.../subscriptions/...）。",
    )
    parser.add_argument(
        "--pubsub-flow-max-messages",
        type=int,
        default=50,
        help="StreamingPull の flow control max_messages。",
    )
    parser.add_argument(
        "--pubsub-pending-warn-threshold",
        "--pubsub-trigger-queue-size",
        type=int,
        dest="pubsub_pending_warn_threshold",
        default=256,
        help="StreamingPull の pending backlog 警告しきい値（旧名 --pubsub-trigger-queue-size 互換）。",
    )
    parser.add_argument(
        "--heartbeat-file",
        type=str,
        help="StreamingPull 用 heartbeat ファイル。相対パスは runtime 基準。",
    )
    parser.add_argument(
        "--heartbeat-interval-seconds",
        type=float,
        help="StreamingPull が heartbeat を更新する間隔秒。",
    )
    parser.add_argument(
        "--heartbeat-max-age-seconds",
        type=float,
        help="fallback-watchdog が異常判定に使う heartbeat 最大許容秒。",
    )
    parser.add_argument(
        "--main-service-name",
        type=str,
        help="fallback-watchdog で監視するメイン系 systemd サービス名。",
    )
    parser.add_argument(
        "--fallback-watchdog",
        action="store_true",
        help="--once 実行時にメイン系を監視し、健全ならポーリングをスキップする。",
    )
    parser.add_argument("--setup-watch", action="store_true", help="Gmail watch を Pub/Sub topic に登録して終了する。")
    parser.add_argument("--pubsub-topic", type=str, help="watch 登録先 topic（projects/.../topics/...）。")
    parser.add_argument(
        "--watch-label-ids",
        type=str,
        default="INBOX",
        help="watch 対象の Gmail label（カンマ区切り、既定は INBOX）。",
    )
    parser.add_argument(
        "--watch-label-filter-action",
        choices=("include", "exclude"),
        default="include",
        help="watch の labelFilterAction。",
    )
    parser.add_argument("--watch-retries", type=int, default=4, help="watch API 登録時の最大リトライ回数。")
    parser.add_argument("--watch-base-delay", type=float, default=1.0, help="watch API 登録リトライの初期待機秒。")
    parser.add_argument("--watch-max-delay", type=float, default=60.0, help="watch API 登録リトライの最大待機秒。")

    args = parser.parse_args()
    app_config.configure_runtime_paths(args.config)

    if handle_reauth(args):
        return

    if handle_health_check(args):
        return

    config = load_config_or_exit()
    validation_errors = validate_config(config)

    if handle_validate_config(args, validation_errors):
        return

    log_path = (
        app_config.resolve_runtime_path(args.log_file)
        if args.log_file
        else app_config.resolve_runtime_path(config.get("log_file", str(app_config.DEFAULT_LOG_PATH)))
    )
    app_config.setup_logging(log_path, structured=bool(config.get("structured_logging", False)))

    if validation_errors:
        app_config.LOGGER.error("CONFIG_INVALID: %s", " | ".join(validation_errors))
        print_validation_errors(validation_errors)
        sys.exit(1)

    if not looks_like_discord_webhook_url(config["discord_webhook_url"]):
        app_config.LOGGER.warning(
            "CONFIG_DISCORD_WEBHOOK_URL_UNUSUAL: value=%s",
            config["discord_webhook_url"],
        )

    if args.test_discord:
        webhook_url = config["discord_webhook_url"]
        sent = send_discord_test(
            webhook_url,
            "Amazon Notify の test-discord コマンドから送信しました。",
        )
        if not sent:
            app_config.LOGGER.error("TEST_DISCORD_FAILED")
            _stderr_error("Discord テスト通知の送信に失敗しました。")
            sys.exit(1)
        app_config.LOGGER.info("TEST_DISCORD_SUCCESS")
        sys.stdout.write("[OK] Discord テスト通知を送信しました。\n")
        return

    if args.setup_watch:
        handle_setup_watch(args, config)
        return

    runtime = build_runtime(config, dry_run=args.dry_run)
    heartbeat_file, heartbeat_interval_seconds, heartbeat_max_age_seconds, main_service_name = (
        resolve_watchdog_options(args, runtime)
    )
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
