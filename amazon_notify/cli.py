import argparse
import json
import sys
import time
from pathlib import Path

from . import config as app_config
from .backoff import next_delay_seconds
from .discord_client import send_discord_alert, send_discord_test
from .failover import evaluate_failover_watchdog
from .gmail_client import (
    get_gmail_service_with_status,
    run_oauth_flow,
    start_gmail_watch_with_retry,
)
from .health import load_config_for_health_check as load_config_for_health_check_impl
from .health import run_health_check as run_health_check_impl
from .notifier import run_once
from .runtime import (
    MIN_POLL_INTERVAL_SECONDS,
    RuntimeConfig,
    looks_like_discord_webhook_url,
)
from .runtime import (
    build_runtime as build_runtime_impl,
)
from .runtime import (
    compile_optional_pattern as compile_optional_pattern_impl,
)
from .runtime import (
    validate_config as validate_config_impl,
)
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
    return load_config_for_health_check_impl(
        app_config.get_runtime_paths(),
        validate_config=validate_config,
    )


def validate_config(config: dict) -> list[str]:
    return validate_config_impl(config, paths=app_config.get_runtime_paths())


def run_health_check(config: dict | None, validation_errors: list[str]) -> int:
    exit_code, report = run_health_check_impl(
        app_config.get_runtime_paths(),
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
    if not args.reauth:
        return False
    log_path = app_config.resolve_runtime_path(args.log_file) if args.log_file else app_config.DEFAULT_LOG_PATH
    app_config.setup_logging(log_path)
    app_config.LOGGER.info("MANUAL_REAUTH_START")
    creds = run_oauth_flow(paths=app_config.get_runtime_paths())
    if not creds:
        app_config.LOGGER.error("MANUAL_REAUTH_FAILED")
        sys.exit(1)
    app_config.LOGGER.info("MANUAL_REAUTH_SUCCESS")
    return True


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
    topic_name = (args.pubsub_topic or "").strip()
    if not topic_name:
        _stderr_error("--setup-watch には --pubsub-topic が必要です。")
        sys.exit(1)

    state_file = app_config.resolve_runtime_path(config.get("state_file", "state.json"))
    state = app_config.load_state(state_file)
    service, status = get_gmail_service_with_status(
        webhook_url=config["discord_webhook_url"],
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
        paths=app_config.get_runtime_paths(),
    )
    if service is None:
        _stderr_error(f"Gmail service を初期化できませんでした。auth_status={status.value}")
        sys.exit(1)

    label_ids = [item.strip() for item in args.watch_label_ids.split(",") if item.strip()]
    try:
        watch_response = start_gmail_watch_with_retry(
            service,
            topic_name=topic_name,
            label_ids=label_ids,
            label_filter_action=args.watch_label_filter_action,
            retries=args.watch_retries,
            base_delay=args.watch_base_delay,
            max_delay=args.watch_max_delay,
        )
    except Exception as exc:
        app_config.LOGGER.error("GMAIL_WATCH_SETUP_FAILED: %s", exc)
        _stderr_error(f"Gmail watch 登録に失敗しました: {exc}")
        sys.exit(1)

    sys.stdout.write(json.dumps(watch_response, ensure_ascii=False, indent=2) + "\n")


def resolve_watchdog_options(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
) -> tuple[Path, float, float, str]:
    heartbeat_file = (
        app_config.resolve_runtime_path(args.heartbeat_file)
        if args.heartbeat_file
        else runtime.pubsub_heartbeat_file
    )
    heartbeat_interval_seconds = (
        float(args.heartbeat_interval_seconds)
        if args.heartbeat_interval_seconds is not None
        else float(runtime.pubsub_heartbeat_interval_seconds)
    )
    heartbeat_max_age_seconds = (
        float(args.heartbeat_max_age_seconds)
        if args.heartbeat_max_age_seconds is not None
        else float(runtime.pubsub_heartbeat_max_age_seconds)
    )
    main_service_name = (
        args.main_service_name.strip()
        if args.main_service_name
        else str(runtime.pubsub_main_service_name)
    )
    return heartbeat_file, heartbeat_interval_seconds, heartbeat_max_age_seconds, main_service_name


def validate_watchdog_options(
    heartbeat_interval_seconds: float,
    heartbeat_max_age_seconds: float,
    main_service_name: str,
) -> None:
    if heartbeat_interval_seconds <= 0:
        _stderr_error("heartbeat-interval-seconds は 0 より大きい値を指定してください。")
        sys.exit(1)
    if heartbeat_max_age_seconds <= 0:
        _stderr_error("heartbeat-max-age-seconds は 0 より大きい値を指定してください。")
        sys.exit(1)
    if not main_service_name:
        _stderr_error("main-service-name は空文字にできません。")
        sys.exit(1)


def handle_streaming_mode(
    args: argparse.Namespace,
    config: dict,
    runtime: RuntimeConfig,
    heartbeat_file: Path,
    heartbeat_interval_seconds: float,
) -> None:

    if args.fallback_watchdog:
        _stderr_error("--streaming-pull と --fallback-watchdog は同時に指定できません。")
        sys.exit(1)
    if args.once:
        _stderr_error("--streaming-pull と --once は同時に指定できません。")
        sys.exit(1)
    if args.interval is not None:
        _stderr_error("--streaming-pull と --interval は同時に指定できません。")
        sys.exit(1)
    subscription = (args.pubsub_subscription or config.get("pubsub_subscription", "")).strip()
    if not subscription:
        _stderr_error("StreamingPull には pubsub subscription が必要です。")
        sys.exit(1)

    app_config.LOGGER.info("STREAMING_PULL_MODE_START: subscription=%s", subscription)
    run_once_with_guard(runtime)
    reconnect_attempt = 0
    reconnect_max_attempts = runtime.pubsub_stream_reconnect_max_attempts
    reconnect_base_delay = runtime.pubsub_stream_reconnect_base_delay_seconds
    reconnect_max_delay = runtime.pubsub_stream_reconnect_max_delay_seconds

    while True:
        try:
            run_streaming_pull(
                subscription_path=subscription,
                on_trigger=lambda: run_once_with_guard(runtime),
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
            if reconnect_max_attempts > 0 and reconnect_attempt >= reconnect_max_attempts:
                _stderr_error(
                    "StreamingPull の再接続試行回数が上限に達しました。"
                    f" attempts={reconnect_attempt}"
                )
                sys.exit(1)

            delay = next_delay_seconds(
                reconnect_attempt,
                base_delay=reconnect_base_delay,
                max_delay=reconnect_max_delay,
            )
            app_config.LOGGER.warning(
                "STREAMING_PULL_RECONNECT_RETRY: attempt=%s wait=%.2fs",
                reconnect_attempt,
                delay,
            )
            time.sleep(delay)


def should_run_fallback_polling(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
    heartbeat_file: Path,
    heartbeat_max_age_seconds: float,
    main_service_name: str,
) -> bool:
    if not args.fallback_watchdog:
        return True
    if not args.once:
        _stderr_error("--fallback-watchdog は --once と併用してください。")
        sys.exit(1)
    return evaluate_failover_watchdog(
        state_file=runtime.state_file,
        discord_webhook_url=runtime.discord_webhook_url,
        service_name=main_service_name,
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=heartbeat_max_age_seconds,
        dry_run=runtime.dry_run,
    )


def run_polling_mode(args: argparse.Namespace, config: dict, runtime: RuntimeConfig) -> None:
    poll_interval = args.interval or int(config.get("poll_interval_seconds", 60))
    if poll_interval < MIN_POLL_INTERVAL_SECONDS:
        _stderr_error(f"interval は {MIN_POLL_INTERVAL_SECONDS} 以上を指定してください。")
        sys.exit(1)

    first_run_ok = run_once_with_guard(runtime)
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
        time.sleep(poll_interval)
        run_once_with_guard(runtime)


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
