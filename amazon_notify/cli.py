from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import config as app_config
from .checkpoint_store import JsonlCheckpointStore
from .commands import health as health_command
from .commands import polling as polling_command
from .commands import reauth as reauth_command
from .commands import retention as retention_command
from .commands import scenario as scenario_command
from .commands import status as status_command
from .commands import streaming as streaming_command
from .commands import verify as verify_command
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


def validate_action_conflicts(args: argparse.Namespace) -> None:
    action_flags = (
        ("--reauth", args.reauth),
        ("--health-check", args.health_check),
        ("--validate-config", args.validate_config),
        ("--test-discord", args.test_discord),
        ("--setup-watch", args.setup_watch),
        ("--rebuild-indexes", args.rebuild_indexes),
        ("--status", args.status),
        ("--doctor", args.doctor),
        ("--verify-state", args.verify_state),
        ("--metrics", args.metrics),
        ("--archive-runtime", args.archive_runtime),
        ("--restore-runtime", args.restore_runtime),
        ("--restore-drill", args.restore_drill),
        ("--scenario-harness", args.scenario_harness),
        ("--streaming-pull", args.streaming_pull),
    )
    selected_actions = [name for name, enabled in action_flags if enabled]
    if len(selected_actions) <= 1:
        return
    joined = ", ".join(selected_actions)
    _stderr_error(
        f"action flags は同時指定できません。1つだけ指定してください: {joined}"
    )
    sys.exit(1)


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


def handle_rebuild_indexes(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.rebuild_indexes:
        return False
    store = JsonlCheckpointStore(
        state_file=runtime.state_file,
        events_file=runtime.events_file,
        runs_file=runtime.runs_file,
    )
    rebuilt = store.rebuild_indexes()
    sys.stdout.write(
        json.dumps(
            {
                "status": "ok",
                "checkpoint_index_rebuilt": rebuilt["checkpoint_index"],
                "run_summary_index_rebuilt": rebuilt["run_summary_index"],
                "events_file": str(runtime.events_file),
                "runs_file": str(runtime.runs_file),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return True


def handle_status_report(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.status:
        return False
    exit_code, report = status_command.build_status_report(runtime)
    sys.stdout.write(status_command.format_status_summary(report) + "\n")
    sys.exit(exit_code)


def handle_doctor_report(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.doctor:
        return False
    exit_code, report = status_command.build_doctor_report(runtime)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.exit(exit_code)


def handle_verify_state_report(
    args: argparse.Namespace, runtime: RuntimeConfig
) -> bool:
    if not args.verify_state:
        return False
    exit_code, report = verify_command.run_verify_state(runtime)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.exit(exit_code)


def handle_metrics_report(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.metrics and not args.metrics_plain:
        return False
    report = status_command.build_metrics_report(
        runtime, recent_run_window=args.metrics_window
    )
    if args.metrics_plain:
        sys.stdout.write(status_command.format_metrics_plain(report) + "\n")
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.exit(0)


def handle_archive_runtime(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.archive_runtime:
        return False
    archive_dir = (
        app_config.resolve_runtime_path(
            args.archive_dir, base_dir=runtime.runtime_paths.runtime_dir
        )
        if args.archive_dir
        else None
    )
    report = retention_command.archive_runtime(
        runtime,
        label=args.archive_label,
        archive_dir=archive_dir,
        gzip_enabled=not args.archive_no_gzip,
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return True


def handle_restore_runtime(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.restore_runtime:
        return False
    if not args.restore_label:
        _stderr_error("--restore-runtime には --restore-label の指定が必要です。")
        sys.exit(1)
    archive_dir = (
        app_config.resolve_runtime_path(
            args.archive_dir, base_dir=runtime.runtime_paths.runtime_dir
        )
        if args.archive_dir
        else None
    )
    exit_code, report = retention_command.restore_runtime(
        runtime,
        label=args.restore_label,
        archive_dir=archive_dir,
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.exit(exit_code)


def handle_restore_drill(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.restore_drill:
        return False
    archive_dir = (
        app_config.resolve_runtime_path(
            args.archive_dir, base_dir=runtime.runtime_paths.runtime_dir
        )
        if args.archive_dir
        else None
    )
    exit_code, report = retention_command.run_restore_drill(
        runtime, archive_dir=archive_dir
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.exit(exit_code)


def handle_scenario_harness(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.scenario_harness:
        return False
    scenario_names = None
    if args.scenario_names:
        scenario_names = [name.strip() for name in args.scenario_names.split(",")]
        scenario_names = [name for name in scenario_names if name]
    exit_code, report = scenario_command.run_scenario_harness(
        runtime, scenario_names=scenario_names
    )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.exit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Amazon配送メールを監視してDiscordに通知"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="設定ファイルの保存先。相対パスはカレントディレクトリ基準。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="1回だけ実行して終了する（デフォルトはループ実行）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="ループ実行時の待ち時間（秒）。configのpoll_interval_secondsより優先される。",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="対話OAuthで token.json を再作成して終了する。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gmail取得と判定のみ実行し、Discord送信とstate更新を行わない。",
    )
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="設定済み Discord Webhook へテスト通知を送信して終了する。",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="設定ファイルを検証して終了する。",
    )
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="events/runs の index snapshot を再構築して終了する。",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="運用サマリ(frontier/incident/failure/整合性)を表示して終了する。",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="state/events/runs/index の整合性診断(JSON)を表示して終了する。",
    )
    parser.add_argument(
        "--verify-state",
        action="store_true",
        help="append-only正本/派生state/index の追加監査(JSON)を実行する。",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="運用メトリクス(JSON 既定)を表示して終了する。",
    )
    parser.add_argument(
        "--metrics-plain",
        action="store_true",
        help="--metrics を簡易テキストで出力する。",
    )
    parser.add_argument(
        "--metrics-window",
        type=int,
        default=50,
        help="--metrics の直近 run 集計に使う最大件数。",
    )
    parser.add_argument(
        "--archive-runtime",
        action="store_true",
        help="events/runs/state/index を archive へ退避する。",
    )
    parser.add_argument(
        "--archive-label",
        type=str,
        help="archive 識別子。未指定時は UTC timestamp。",
    )
    parser.add_argument(
        "--archive-dir",
        type=str,
        help="archive 保存先。相対パスは runtime 基準。",
    )
    parser.add_argument(
        "--archive-no-gzip",
        action="store_true",
        help="archive 時に gzip 圧縮しない。",
    )
    parser.add_argument(
        "--restore-runtime",
        action="store_true",
        help="archive から events/runs/state/index を復元する。",
    )
    parser.add_argument(
        "--restore-label",
        type=str,
        help="restore 対象 archive 識別子。",
    )
    parser.add_argument(
        "--restore-drill",
        action="store_true",
        help="一時ディレクトリで archive/restore drill を実行する。",
    )
    parser.add_argument(
        "--scenario-harness",
        action="store_true",
        help="fault-injection シナリオを実行して設計耐性を検証する。",
    )
    parser.add_argument(
        "--scenario-names",
        type=str,
        help="scenario-harness の対象名をカンマ区切りで指定する。",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="実行前提のヘルスチェック結果をJSONで出力して終了する。",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        help="ログファイルの保存先（未指定時は logs/amazon_mail_notifier.log）。",
    )
    parser.add_argument(
        "--streaming-pull",
        action="store_true",
        help="Pub/Sub StreamingPull でイベント駆動実行する。",
    )
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
    parser.add_argument(
        "--setup-watch",
        action="store_true",
        help="Gmail watch を Pub/Sub topic に登録して終了する。",
    )
    parser.add_argument(
        "--pubsub-topic",
        type=str,
        help="watch 登録先 topic（projects/.../topics/...）。",
    )
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
    parser.add_argument(
        "--watch-retries",
        type=int,
        default=4,
        help="watch API 登録時の最大リトライ回数。",
    )
    parser.add_argument(
        "--watch-base-delay",
        type=float,
        default=1.0,
        help="watch API 登録リトライの初期待機秒。",
    )
    parser.add_argument(
        "--watch-max-delay",
        type=float,
        default=60.0,
        help="watch API 登録リトライの最大待機秒。",
    )

    args = parser.parse_args()
    validate_action_conflicts(args)
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
            config["discord_webhook_url"],
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

    if handle_rebuild_indexes(args, runtime):
        return
    if handle_status_report(args, runtime):
        return
    if handle_doctor_report(args, runtime):
        return
    if handle_verify_state_report(args, runtime):
        return
    if handle_metrics_report(args, runtime):
        return
    if handle_archive_runtime(args, runtime):
        return
    if handle_restore_runtime(args, runtime):
        return
    if handle_restore_drill(args, runtime):
        return
    if handle_scenario_harness(args, runtime):
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
