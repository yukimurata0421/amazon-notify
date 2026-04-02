import argparse
import re
import sys
import time

from . import config as app_config
from .discord_client import send_discord_alert
from .gmail_client import run_oauth_flow
from .notifier import run_once


def compile_optional_pattern(pattern: str | None, config_key: str) -> re.Pattern[str] | None:
    if not pattern:
        return None

    try:
        return re.compile(pattern)
    except re.error as exc:
        app_config.LOGGER.error("CONFIG_INVALID_REGEX: %s=%r error=%s", config_key, pattern, exc)
        sys.stderr.write(f"[ERROR] config.json の {config_key} が不正な正規表現です: {exc}\n")
        sys.exit(1)


def build_runtime(config: dict) -> dict:
    return {
        "discord_webhook_url": config["discord_webhook_url"],
        "amazon_pattern": config.get("amazon_from_pattern", r"amazon\\.co\\.jp"),
        "state_file": app_config.resolve_runtime_path(config.get("state_file", "state.json")),
        "max_messages": int(config.get("max_messages", 50)),
        "subject_pattern": compile_optional_pattern(
            config.get("amazon_subject_pattern"),
            "amazon_subject_pattern",
        ),
    }


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
    parser.add_argument("--log-file", type=str, help="ログファイルの保存先（未指定時は logs/amazon_mail_notifier.log）。")
    args = parser.parse_args()
    app_config.configure_runtime_paths(args.config)

    if args.reauth:
        log_path = app_config.resolve_runtime_path(args.log_file) if args.log_file else app_config.DEFAULT_LOG_PATH
        app_config.setup_logging(log_path)
        app_config.LOGGER.info("MANUAL_REAUTH_START")
        creds = run_oauth_flow()
        if not creds:
            app_config.LOGGER.error("MANUAL_REAUTH_FAILED")
            sys.exit(1)
        app_config.LOGGER.info("MANUAL_REAUTH_SUCCESS")
        return

    if not app_config.CONFIG_PATH.exists():
        app_config.LOGGER.error("CONFIG_MISSING: %s", app_config.CONFIG_PATH)
        sys.stderr.write(f"[ERROR] {app_config.CONFIG_PATH} が見つかりません。\n")
        sys.exit(1)

    config = app_config.load_config(app_config.CONFIG_PATH)
    log_path = (
        app_config.resolve_runtime_path(args.log_file)
        if args.log_file
        else app_config.resolve_runtime_path(config.get("log_file", str(app_config.DEFAULT_LOG_PATH)))
    )
    app_config.setup_logging(log_path)

    if not config.get("discord_webhook_url"):
        app_config.LOGGER.error("CONFIG_INVALID: discord_webhook_url is missing")
        sys.stderr.write("[ERROR] config.json に discord_webhook_url が設定されていません。\n")
        sys.exit(1)

    runtime = build_runtime(config)
    poll_interval = args.interval or int(config.get("poll_interval_seconds", 60))

    run_once(runtime)
    if args.once:
        return

    app_config.LOGGER.info("LOOP_MODE_START: interval=%ss", poll_interval)
    while True:
        time.sleep(poll_interval)
        try:
            run_once(runtime)
        except Exception as exc:
            app_config.LOGGER.exception("RUN_ONCE_UNHANDLED_EXCEPTION: %s", exc)
            if runtime["discord_webhook_url"]:
                send_discord_alert(
                    runtime["discord_webhook_url"],
                    f"未処理例外を検知しました。次周期で再試行します。\nエラー: {exc}",
                )
