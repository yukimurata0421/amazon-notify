import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import config as app_config
from .discord_client import send_discord_alert, send_discord_test
from .gmail_client import run_oauth_flow
from .notifier import run_once

DEFAULT_LOG_FILE_RELATIVE = "logs/amazon_mail_notifier.log"


def compile_optional_pattern(pattern: str | None, config_key: str) -> re.Pattern[str] | None:
    if not pattern:
        return None

    try:
        return re.compile(pattern)
    except re.error as exc:
        app_config.LOGGER.error("CONFIG_INVALID_REGEX: %s=%r error=%s", config_key, pattern, exc)
        sys.stderr.write(f"[ERROR] config.json の {config_key} が不正な正規表現です: {exc}\n")
        sys.exit(1)


def build_runtime(config: dict, dry_run: bool = False) -> dict:
    return {
        "discord_webhook_url": config["discord_webhook_url"],
        "amazon_pattern": config.get("amazon_from_pattern", r"amazon\.co\.jp"),
        "state_file": app_config.resolve_runtime_path(config.get("state_file", "state.json")),
        "max_messages": int(config.get("max_messages", 50)),
        "dry_run": dry_run,
        "subject_pattern": compile_optional_pattern(
            config.get("amazon_subject_pattern"),
            "amazon_subject_pattern",
        ),
    }


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
    if not app_config.CONFIG_PATH.exists():
        return None, [f"{app_config.CONFIG_PATH} が見つかりません。"]

    try:
        config = app_config.load_config(app_config.CONFIG_PATH)
    except json.JSONDecodeError as exc:
        return None, [f"config.json の JSON が不正です: {exc}"]
    except OSError as exc:
        return None, [f"config.json を読み込めませんでした: {exc}"]

    return config, validate_config(config)


def validate_config(config: dict) -> list[str]:
    errors: list[str] = []

    webhook = config.get("discord_webhook_url")
    if not isinstance(webhook, str) or not webhook.strip():
        errors.append("discord_webhook_url が未設定です。")

    for key in ("max_messages", "poll_interval_seconds"):
        if key not in config:
            continue
        try:
            value = int(config[key])
        except (TypeError, ValueError):
            errors.append(f"{key} は整数で指定してください。")
            continue
        if value <= 0:
            errors.append(f"{key} は 1 以上を指定してください。")

    amazon_from_pattern = config.get("amazon_from_pattern", r"amazon\.co\.jp")
    try:
        re.compile(amazon_from_pattern)
    except re.error as exc:
        errors.append(f"amazon_from_pattern の正規表現が不正です: {exc}")

    subject_pattern = config.get("amazon_subject_pattern")
    if subject_pattern:
        try:
            re.compile(subject_pattern)
        except re.error as exc:
            errors.append(f"amazon_subject_pattern の正規表現が不正です: {exc}")

    for key, default_value in (("state_file", "state.json"), ("log_file", DEFAULT_LOG_FILE_RELATIVE)):
        value = config.get(key, default_value)
        if not isinstance(value, str):
            errors.append(f"{key} は空文字以外の文字列で指定してください。")
            continue
        if not value.strip():
            errors.append(f"{key} は空文字以外の文字列で指定してください。")

    return errors


def run_health_check(config: dict | None, validation_errors: list[str]) -> int:
    checks: list[dict[str, str | bool]] = []

    checks.append(
        {
            "name": "config_file_exists",
            "ok": app_config.CONFIG_PATH.exists(),
            "detail": str(app_config.CONFIG_PATH),
        }
    )
    checks.append(
        {
            "name": "config_valid",
            "ok": not validation_errors,
            "detail": "OK" if not validation_errors else " / ".join(validation_errors),
        }
    )
    checks.append(
        {
            "name": "credentials_file_exists",
            "ok": app_config.CREDENTIALS_PATH.exists(),
            "detail": str(app_config.CREDENTIALS_PATH),
        }
    )
    checks.append(
        {
            "name": "token_file_exists",
            "ok": app_config.TOKEN_PATH.exists(),
            "detail": str(app_config.TOKEN_PATH),
        }
    )

    state_file: Path | None = None
    log_file: Path | None = None
    if config is not None:
        state_file = app_config.resolve_runtime_path(config.get("state_file", "state.json"))
        log_file = app_config.resolve_runtime_path(
            config.get("log_file", str(app_config.DEFAULT_LOG_PATH))
        )

    status = "ok" if all(bool(check["ok"]) for check in checks) else "degraded"
    report = {
        "status": status,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runtime_dir": str(app_config.RUNTIME_DIR),
        "config_path": str(app_config.CONFIG_PATH),
        "state_file": str(state_file) if state_file else None,
        "log_file": str(log_file) if log_file else None,
        "checks": checks,
    }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if status == "ok" else 1


def print_validation_errors(errors: list[str]) -> None:
    for err in errors:
        _stderr_error(err)


def run_once_with_guard(runtime: dict) -> bool:
    try:
        run_once(runtime)
        return True
    except Exception as exc:
        app_config.LOGGER.exception("RUN_ONCE_UNHANDLED_EXCEPTION: %s", exc)
        if runtime["discord_webhook_url"] and not runtime["dry_run"]:
            send_discord_alert(
                runtime["discord_webhook_url"],
                f"未処理例外を検知しました。次周期で再試行します。\nエラー: {exc}",
            )
        return False


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

    if args.health_check:
        health_config, health_validation_errors = load_config_for_health_check()
        sys.exit(run_health_check(health_config, health_validation_errors))

    config = load_config_or_exit()
    validation_errors = validate_config(config)

    if args.validate_config:
        if validation_errors:
            print_validation_errors(validation_errors)
            sys.exit(1)
        sys.stdout.write("[OK] config.json の検証に成功しました。\n")
        return

    log_path = (
        app_config.resolve_runtime_path(args.log_file)
        if args.log_file
        else app_config.resolve_runtime_path(config.get("log_file", str(app_config.DEFAULT_LOG_PATH)))
    )
    app_config.setup_logging(log_path)

    if validation_errors:
        app_config.LOGGER.error("CONFIG_INVALID: %s", " | ".join(validation_errors))
        print_validation_errors(validation_errors)
        sys.exit(1)

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

    runtime = build_runtime(config, dry_run=args.dry_run)
    poll_interval = args.interval or int(config.get("poll_interval_seconds", 60))
    if poll_interval <= 0:
        _stderr_error("interval は 1 以上を指定してください。")
        sys.exit(1)

    first_run_ok = run_once_with_guard(runtime)
    if args.once:
        if not first_run_ok:
            sys.exit(1)
        return

    app_config.LOGGER.info(
        "LOOP_MODE_START: interval=%ss dry_run=%s",
        poll_interval,
        runtime["dry_run"],
    )
    while True:
        time.sleep(poll_interval)
        run_once_with_guard(runtime)
