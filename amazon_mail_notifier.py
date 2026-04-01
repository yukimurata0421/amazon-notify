#!/usr/bin/env python3
"""
Gmail から Amazon 関連のメールを拾って Discord に通知するスクリプト
(Auto-Refresh & Error Notification 版)

- Gmail API (credentials.json / token.json) を使用
- config.json で Discord Webhook URL や Amazon 判定用の正規表現を設定
- state.json に「最後に処理した messageId」を保存して重複通知を防ぐ

【改良点】
- トークンの期限切れ時に自動でリフレッシュを試みる
- リフレッシュに失敗した場合（有効期限切れなど）、Discordに警告を通知して終了する
"""

import argparse
import json
import logging
import re
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from email.header import decode_header, make_header
from typing import Any

import requests

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    GOOGLE_IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:
    Credentials = Any  # type: ignore[assignment]
    build = None  # type: ignore[assignment]
    HttpError = Exception  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    InstalledAppFlow = None  # type: ignore[assignment]
    GOOGLE_IMPORT_ERROR = exc

# =========================
# パス・定数まわり
# =========================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"
DEFAULT_LOG_PATH = BASE_DIR / "logs" / "amazon_mail_notifier.log"

# Gmail API のスコープ（読み取り専用）
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LOGGER = logging.getLogger("amazon_mail_notifier")


def ensure_google_dependencies() -> None:
    """Google API 依存が未導入なら明示的にエラーにする"""
    if GOOGLE_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "Google API libraries are missing. Install runtime deps with: "
            "`pip install -r requirements.txt`"
        ) from GOOGLE_IMPORT_ERROR


def setup_logging(log_path: Path = DEFAULT_LOG_PATH) -> None:
    """標準出力 + ローテートファイルへログを記録する"""
    if LOGGER.handlers:
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    LOGGER.propagate = False


# =========================
# ユーティリティ
# =========================

def run_oauth_flow() -> Credentials | None:
    """
    ブラウザ/コンソール経由で新規 OAuth トークンを取得し token.json に保存する。
    headless 環境でブラウザが開けなくても console フローに自動でフォールバックする。
    """
    try:
        ensure_google_dependencies()
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH),
            SCOPES,
        )
    except Exception as e:
        LOGGER.error("OAUTH_PREPARE_FAILED: %s", e)
        return None

    try:
        creds = flow.run_local_server(port=0)
    except Exception as e:
        LOGGER.warning("OAUTH_LOCAL_SERVER_FAILED: %s", e)
        try:
            creds = flow.run_console()
        except Exception as e2:
            LOGGER.error("OAUTH_CONSOLE_FAILED: %s", e2)
            return None

    with TOKEN_PATH.open("w", encoding="utf-8") as token:
        token.write(creds.to_json())
        LOGGER.info("TOKEN_SAVED: %s", TOKEN_PATH)

    return creds


def load_config(path: Path) -> dict:
    """config.json を読み込む"""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    """state.json を読み込む（なければデフォルト値）"""
    if not path.exists():
        return {"last_message_id": None}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    """state.json に保存する"""
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def resolve_runtime_path(path_value: str | Path) -> Path:
    """相対パスは BASE_DIR 基準で絶対化する"""
    p = Path(path_value)
    return p if p.is_absolute() else BASE_DIR / p


def decode_mime_words(s: str) -> str:
    """=?UTF-8?...?= みたいな MIME エンコード文字列をデコード"""
    try:
        dh = decode_header(s)
        return str(make_header(dh))
    except Exception:
        return s


def extract_email_address(s: str) -> str:
    """
    From ヘッダーなどからメールアドレスだけ抜き出す
    例: 'Amazon.co.jp <shipment-tracking@amazon.co.jp>' → 'shipment-tracking@amazon.co.jp'
    """
    decoded = decode_mime_words(s)
    m = re.search(r'[\w\.-]+@[\w\.-]+', decoded)
    return m.group(0) if m else decoded


# =========================
# Discord 通知 (エラー用)
# =========================

def send_discord_alert(webhook_url: str, message: str) -> bool:
    """システムエラーや警告をDiscordに通知する"""
    if not webhook_url:
        return False

    content = f"⚠️ **Gmail監視システム警告**\n{message}"
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        LOGGER.error("DISCORD_ALERT_FAILED: %s", e)
        return False


def send_discord_recovery(webhook_url: str, message: str) -> bool:
    """一時障害からの復旧をDiscordに通知する"""
    if not webhook_url:
        return False

    content = f"✅ **Gmail監視システム復旧**\n{message}"
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        LOGGER.error("DISCORD_RECOVERY_FAILED: %s", e)
        return False


def mark_transient_network_issue(state: dict, state_file: Path, err: Exception | str) -> None:
    """一時的な通信障害を state に記録する"""
    state["transient_network_issue_active"] = True
    state["last_transient_error"] = str(err)
    state["last_transient_error_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_file, state)


def notify_recovery_if_needed(webhook_url: str, state: dict, state_file: Path) -> None:
    """障害継続フラグが立っていた場合、復旧通知を1回だけ送る"""
    if not state.get("transient_network_issue_active"):
        return

    last_error = state.get("last_transient_error", "(unknown)")
    last_error_at = state.get("last_transient_error_at", "(unknown)")
    message = (
        "一時的な通信障害から復旧しました。Gmail監視を再開しています。\n"
        f"前回障害時刻: {last_error_at}\n"
        f"前回エラー: {last_error}"
    )
    if not send_discord_recovery(webhook_url, message):
        LOGGER.warning("TRANSIENT_RECOVERY_NOTIFICATION_SKIPPED")
        return

    state["transient_network_issue_active"] = False
    state.pop("last_transient_error", None)
    state.pop("last_transient_error_at", None)
    save_state(state_file, state)


def mark_token_issue(state: dict, state_file: Path, reason: str) -> bool:
    """
    token 問題を state に記録する。
    戻り値: True のときのみ通知を送る（初回/理由変更時）。
    """
    previous_active = bool(state.get("token_issue_active"))
    previous_reason = state.get("token_issue_reason")

    state["token_issue_active"] = True
    state["token_issue_reason"] = reason
    state["token_issue_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_file, state)

    return (not previous_active) or (previous_reason != reason)


def notify_token_recovery_if_needed(webhook_url: str, state: dict, state_file: Path) -> None:
    """token 問題が解消したときに復旧通知を1回だけ送る"""
    if not state.get("token_issue_active"):
        return

    last_reason = state.get("token_issue_reason", "(unknown)")
    issue_at = state.get("token_issue_at", "(unknown)")
    message = (
        "token 問題から復旧しました。監視を再開しています。\n"
        f"前回障害時刻: {issue_at}\n"
        f"前回理由: {last_reason}"
    )
    if not send_discord_recovery(webhook_url, message):
        LOGGER.warning("TOKEN_RECOVERY_NOTIFICATION_SKIPPED")
        return

    LOGGER.info("TOKEN_RECOVERED: %s", message.replace("\n", " | "))

    state["token_issue_active"] = False
    state.pop("token_issue_reason", None)
    state.pop("token_issue_at", None)
    save_state(state_file, state)


def is_transient_network_error(exc: Exception) -> bool:
    """
    一時的な通信障害かどうかを判定する。
    DNS不調、タイムアウト、TLSハンドシェイク不整合(一時的な経路異常含む)を対象にする。
    """
    if isinstance(exc, (TimeoutError, socket.timeout, socket.gaierror)):
        return True

    transient_keywords = (
        "temporary failure in name resolution",
        "timed out",
        "max retries exceeded",
        "connection aborted",
        "connection reset",
        "certificate verify failed",
        "hostname mismatch",
        "servernotfounderror",
    )

    current = exc
    visited = set()
    while current and id(current) not in visited:
        visited.add(id(current))
        text = f"{type(current).__name__}: {current}".lower()
        if any(keyword in text for keyword in transient_keywords):
            return True
        current = current.__cause__ or current.__context__

    return False


def refresh_with_retry(creds: Credentials, retries: int = 3, base_delay: int = 2) -> Exception | None:
    """
    トークン更新をリトライ付きで実行する。
    成功時は None、失敗時は最終例外を返す。
    """
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            creds.refresh(Request())
            return None
        except Exception as e:
            last_exc = e
            if not is_transient_network_error(e) or attempt == retries:
                return last_exc

            sleep_sec = base_delay * attempt
            LOGGER.warning(
                "TOKEN_REFRESH_RETRY: attempt=%s/%s error=%s retry_in=%ss",
                attempt,
                retries,
                e,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    return last_exc


# =========================
# Gmail API 関連 (強化版)
# =========================

def get_gmail_service(
    webhook_url: str | None = None,
    state: dict | None = None,
    state_file: Path | None = None,
    allow_oauth_interactive: bool = False,
):
    """
    Gmail API の service オブジェクトを返す
    トークンの自動更新を行い、失敗した場合はDiscordに通知する。
    allow_oauth_interactive=False の場合、token 不在時に自動 OAuth は起動しない。
    """
    creds = None
    try:
        ensure_google_dependencies()
    except ModuleNotFoundError as e:
        LOGGER.error("DEPENDENCY_MISSING: %s", e)
        return None

    if not TOKEN_PATH.exists():
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_MISSING_INTERACTIVE_AUTH_START")
            creds = run_oauth_flow()
            if not creds:
                return None
        else:
            reason = f"token.json が見つかりません: {TOKEN_PATH}"
            LOGGER.error("TOKEN_MISSING: %s", reason)
            if webhook_url and state is not None and state_file is not None:
                should_alert = mark_token_issue(state, state_file, reason)
                if should_alert:
                    send_discord_alert(
                        webhook_url,
                        "token.json が存在しないため Gmail API に接続できません。"
                        " `python amazon_mail_notifier.py --reauth` で再認証してください。",
                    )
            return None
    else:
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception as e:
            reason = f"token.json の読み込みに失敗: {e}"
            LOGGER.error("TOKEN_INVALID: %s", reason)
            if allow_oauth_interactive:
                LOGGER.info("TOKEN_INVALID_INTERACTIVE_AUTH_START")
                creds = run_oauth_flow()
            else:
                creds = None
                if webhook_url and state is not None and state_file is not None:
                    should_alert = mark_token_issue(state, state_file, reason)
                    if should_alert:
                        send_discord_alert(
                            webhook_url,
                            "token.json の読み込みに失敗しました。"
                            " `python amazon_mail_notifier.py --reauth` で再認証してください。",
                        )
                return None

    # トークンがない、または無効（期限切れ）の場合
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            LOGGER.info("TOKEN_REFRESH_START")
            refresh_error = refresh_with_retry(creds)
            if refresh_error is None:
                with TOKEN_PATH.open("w", encoding="utf-8") as token:
                    token.write(creds.to_json())
                LOGGER.info("TOKEN_REFRESH_SUCCESS: %s", TOKEN_PATH)
            elif is_transient_network_error(refresh_error):
                error_msg = (
                    "トークン更新時に一時的な通信障害が発生しました。"
                    "今回の実行はスキップし、次周期で自動復旧を待ちます。\n"
                    f"エラー: {refresh_error}"
                )
                LOGGER.warning("TOKEN_REFRESH_TRANSIENT_FAILURE: %s", refresh_error)
                if webhook_url:
                    send_discord_alert(webhook_url, error_msg)
                if state is not None and state_file is not None:
                    mark_transient_network_issue(state, state_file, refresh_error)
                return None
            else:
                reason = f"トークンの自動更新に失敗: {refresh_error}"
                LOGGER.error("TOKEN_REFRESH_FAILED: %s", reason)
                error_msg = (
                    "トークンの自動更新に失敗しました。"
                    " `python amazon_mail_notifier.py --reauth` で再認証してください。\n"
                    f"エラー: {refresh_error}"
                )
                LOGGER.error("TOKEN_REFRESH_FATAL_FAILURE: %s", refresh_error)
                if allow_oauth_interactive:
                    if webhook_url:
                        send_discord_alert(webhook_url, error_msg)
                    creds = run_oauth_flow()
                    if not creds:
                        return None
                else:
                    if webhook_url and state is not None and state_file is not None:
                        should_alert = mark_token_issue(state, state_file, reason)
                        if should_alert:
                            send_discord_alert(webhook_url, error_msg)
                    return None
        else:
            # 無効トークンだが refresh_token が使えないケース
            reason = "token が無効で refresh_token も利用できません"
            LOGGER.error("TOKEN_INVALID_NO_REFRESH: %s", reason)
            if allow_oauth_interactive:
                LOGGER.info("TOKEN_INVALID_NO_REFRESH_INTERACTIVE_AUTH_START")
                creds = run_oauth_flow()
                if not creds:
                    return None
            else:
                if webhook_url and state is not None and state_file is not None:
                    should_alert = mark_token_issue(state, state_file, reason)
                    if should_alert:
                        send_discord_alert(
                            webhook_url,
                            "token が無効で自動更新できません。"
                            " `python amazon_mail_notifier.py --reauth` で再認証してください。",
                        )
                return None

    try:
        service = build("gmail", "v1", credentials=creds)
        if state is not None and state_file is not None:
            notify_token_recovery_if_needed(webhook_url, state, state_file)
        return service
    except Exception as e:
        if is_transient_network_error(e):
            error_msg = (
                "Gmail API service 初期化時に一時的な通信障害が発生しました。"
                "次周期で再試行します。\n"
                f"エラー: {e}"
            )
            LOGGER.warning("GMAIL_SERVICE_TRANSIENT_FAILURE: %s", e)
            if webhook_url:
                send_discord_alert(webhook_url, error_msg)
            if state is not None and state_file is not None:
                mark_transient_network_issue(state, state_file, e)
            return None
        LOGGER.error("GMAIL_SERVICE_BUILD_FAILED: %s", e)
        return None


def list_recent_messages(service, query: str, max_results: int):
    """条件に合う最近のメッセージ一覧を取得"""
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()

    return result.get("messages", [])


def get_message_detail(service, message_id: str) -> dict:
    """messageId から詳細情報を取得"""
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()


# =========================
# Discord 通知 (メール用)
# =========================

def send_discord_notification(
    webhook_url: str,
    subject: str,
    from_addr: str,
    snippet: str,
    url: str,
) -> bool:
    """Discord Webhook にシンプルな埋め込みで通知"""
    content = (
        "📦 **Amazon 配達関連メールを検出しました**\n\n"
        f"**件名**: {subject}\n"
        f"**From**: {from_addr}\n"
        f"**プレビュー**: {snippet}\n"
        f"<{url}>"
    )

    payload = {"content": content}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        LOGGER.info("DISCORD_NOTIFICATION_SENT")
        return True
    except Exception as e:
        LOGGER.error("DISCORD_NOTIFICATION_FAILED: %s", e)
        return False


# =========================
# メインロジック
# =========================

def is_amazon_mail(from_header: str, pattern: str) -> bool:
    """差出人ヘッダー判定"""
    email = extract_email_address(from_header).lower()
    return re.search(pattern, email) is not None


def build_gmail_message_url(message_id: str) -> str:
    """Gmail メール URL 生成"""
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"


def run_once(runtime: dict):
    """
    メールを一度だけチェックして通知する。
    runtime には事前に整形済みの設定を渡す。
    """
    discord_webhook_url = runtime["discord_webhook_url"]
    amazon_pattern = runtime["amazon_pattern"]
    state_file = runtime["state_file"]
    max_messages = runtime["max_messages"]
    subject_pattern = runtime["subject_pattern"]

    state = load_state(state_file)
    last_message_id = state.get("last_message_id")
    LOGGER.info("RUN_ONCE_START: last_message_id=%s", last_message_id)

    # ---- Gmail service 初期化 (更新処理付き) ----
    # Webhook URLを渡して、認証失敗時に通知できるようにする
    service = get_gmail_service(
        webhook_url=discord_webhook_url,
        state=state,
        state_file=state_file,
    )
    
    if service is None:
        LOGGER.warning("RUN_ONCE_SKIPPED: gmail_service_unavailable")
        return

    # ---- メッセージ取得 ----
    query = "in:inbox"

    try:
        messages = list_recent_messages(
            service, query=query, max_results=max_messages
        )
    except HttpError as e:
        LOGGER.error("GMAIL_API_HTTP_ERROR: %s", e)
        if discord_webhook_url:
            send_discord_alert(webhook_url=discord_webhook_url, message=f"Gmail API 呼び出しエラー: {e}")
        return
    except Exception as e:
        if is_transient_network_error(e):
            LOGGER.warning("GMAIL_API_TRANSIENT_ERROR: %s", e)
            if discord_webhook_url:
                send_discord_alert(
                    webhook_url=discord_webhook_url,
                    message=f"Gmail API 取得で一時的な通信障害が発生しました。次周期で再試行します。\nエラー: {e}",
                )
            mark_transient_network_issue(state, state_file, e)
            return
        LOGGER.error("GMAIL_API_UNEXPECTED_ERROR: %s", e)
        if discord_webhook_url:
            send_discord_alert(webhook_url=discord_webhook_url, message=f"Gmail API 予期しないエラー: {e}")
        return

    notify_recovery_if_needed(
        webhook_url=discord_webhook_url,
        state=state,
        state_file=state_file,
    )

    if not messages:
        LOGGER.info("RUN_ONCE_NO_MESSAGES")
        return

    pending_messages: list[dict[str, str]] = []
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        if last_message_id and msg_id == last_message_id:
            break
        pending_messages.append(msg_meta)

    if not pending_messages:
        LOGGER.info("RUN_ONCE_NO_NEW_MESSAGES")
        return

    pending_messages.reverse()
    last_processed_id = last_message_id
    processed_any = False

    for msg_meta in pending_messages:
        msg_id = msg_meta["id"]

        # ---- メッセージ詳細を取得 ----
        try:
            msg = get_message_detail(service, msg_id)
        except Exception as e:
            LOGGER.warning("MESSAGE_DETAIL_FETCH_FAILED: id=%s error=%s", msg_id, e)
            if discord_webhook_url:
                send_discord_alert(
                    webhook_url=discord_webhook_url,
                    message=f"メッセージ詳細の取得に失敗しました。次周期で再試行します。\nmessage_id: {msg_id}\nエラー: {e}",
                )
            break

        headers = msg.get("payload", {}).get("headers", [])
        snippet = msg.get("snippet", "")

        header_dict = {h["name"]: h["value"] for h in headers}
        subject_raw = header_dict.get("Subject", "(no subject)")
        from_raw = header_dict.get("From", "(unknown)")

        subject = decode_mime_words(subject_raw)
        from_decoded = decode_mime_words(from_raw)

        # ---- Amazon 判定 ----
        should_notify = is_amazon_mail(from_decoded, amazon_pattern)
        if should_notify and subject_pattern is not None:
            should_notify = subject_pattern.search(subject) is not None

        if should_notify:
            # ---- Discord に通知 ----
            url = build_gmail_message_url(msg_id)
            LOGGER.info("AMAZON_MAIL_DETECTED: id=%s subject=%s from=%s", msg_id, subject, from_decoded)
            sent = send_discord_notification(
                webhook_url=discord_webhook_url,
                subject=subject,
                from_addr=extract_email_address(from_decoded),
                snippet=snippet,
                url=url,
            )
            if not sent:
                if discord_webhook_url:
                    send_discord_alert(
                        webhook_url=discord_webhook_url,
                        message=(
                            "Amazon メールの Discord 通知に失敗しました。"
                            " state は更新していないため、次周期で再試行します。\n"
                            f"message_id: {msg_id}"
                        ),
                    )
                break
            processed_any = True

        last_processed_id = msg_id

    # ---- state.json の更新 ----
    if last_processed_id and last_processed_id != last_message_id:
        state["last_message_id"] = last_processed_id
        save_state(state_file, state)
        LOGGER.info("STATE_UPDATED: last_message_id=%s", last_processed_id)
    else:
        LOGGER.info("STATE_UNCHANGED")

    if not processed_any:
        LOGGER.info("RUN_ONCE_COMPLETE: amazon_notifications=0")
    else:
        LOGGER.info("RUN_ONCE_COMPLETE: amazon_notifications>=1")


def main():
    parser = argparse.ArgumentParser(
        description="Amazon配送メールを監視してDiscordに通知"
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
        "--log-file",
        type=str,
        help="ログファイルの保存先（未指定時は logs/amazon_mail_notifier.log）。",
    )
    args = parser.parse_args()

    if args.reauth:
        log_path = resolve_runtime_path(args.log_file) if args.log_file else DEFAULT_LOG_PATH
        setup_logging(log_path)
        LOGGER.info("MANUAL_REAUTH_START")
        creds = run_oauth_flow()
        if not creds:
            LOGGER.error("MANUAL_REAUTH_FAILED")
            sys.exit(1)
        LOGGER.info("MANUAL_REAUTH_SUCCESS")
        return

    # ---- 設定読み込み ----
    if not CONFIG_PATH.exists():
        LOGGER.error("CONFIG_MISSING: %s", CONFIG_PATH)
        sys.stderr.write(f"[ERROR] {CONFIG_PATH} が見つかりません。\n")
        sys.exit(1)

    config = load_config(CONFIG_PATH)
    log_path = (
        resolve_runtime_path(args.log_file)
        if args.log_file
        else resolve_runtime_path(config.get("log_file", str(DEFAULT_LOG_PATH)))
    )
    setup_logging(log_path)
    discord_webhook_url = config.get("discord_webhook_url")

    if not discord_webhook_url:
        LOGGER.error("CONFIG_INVALID: discord_webhook_url is missing")
        sys.stderr.write("[ERROR] config.json に discord_webhook_url が設定されていません。\n")
        sys.exit(1)

    runtime = {
        "discord_webhook_url": discord_webhook_url,
        "amazon_pattern": config.get("amazon_from_pattern", r"amazon\\.co\\.jp"),
        "state_file": resolve_runtime_path(config.get("state_file", "state.json")),
        "max_messages": int(config.get("max_messages", 50)),
        "subject_pattern": (
            re.compile(config.get("amazon_subject_pattern"))
            if config.get("amazon_subject_pattern")
            else None
        ),
    }
    poll_interval = args.interval or int(config.get("poll_interval_seconds", 60))

    run_once(runtime)
    if args.once:
        return

    LOGGER.info("LOOP_MODE_START: interval=%ss", poll_interval)
    while True:
        time.sleep(poll_interval)
        try:
            run_once(runtime)
        except Exception as e:
            LOGGER.exception("RUN_ONCE_UNHANDLED_EXCEPTION: %s", e)
            if discord_webhook_url:
                send_discord_alert(
                    webhook_url=discord_webhook_url,
                    message=f"未処理例外を検知しました。次周期で再試行します。\nエラー: {e}",
                )


if __name__ == "__main__":
    main()
