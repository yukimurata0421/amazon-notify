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
import re
import socket
import sys
import time
from email.header import decode_header, make_header
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# =========================
# パス・定数まわり
# =========================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

# Gmail API のスコープ（読み取り専用）
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# =========================
# ユーティリティ
# =========================

def run_oauth_flow() -> Credentials | None:
    """
    ブラウザ/コンソール経由で新規 OAuth トークンを取得し token.json に保存する。
    headless 環境でブラウザが開けなくても console フローに自動でフォールバックする。
    """
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH),
            SCOPES,
        )
    except Exception as e:
        print(f"[ERROR] OAuth フローの準備に失敗: {e}")
        return None

    try:
        creds = flow.run_local_server(port=0)
    except Exception as e:
        print(f"[WARN] ブラウザフローが起動できませんでした。コンソールフローに切り替えます: {e}")
        try:
            creds = flow.run_console()
        except Exception as e2:
            print(f"[ERROR] コンソールフローでも認証できませんでした: {e2}")
            return None

    with TOKEN_PATH.open("w", encoding="utf-8") as token:
        token.write(creds.to_json())
        print("[INFO] 新しい token.json を保存しました。")

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

def send_discord_alert(webhook_url: str, message: str):
    """システムエラーや警告をDiscordに通知する"""
    if not webhook_url:
        return
    
    content = f"⚠️ **Gmail監視システム警告**\n{message}"
    try:
        requests.post(webhook_url, json={"content": content}, timeout=10)
    except Exception as e:
        print(f"[ERROR] Discordへの警告送信失敗: {e}")


def send_discord_recovery(webhook_url: str, message: str):
    """一時障害からの復旧をDiscordに通知する"""
    if not webhook_url:
        return

    content = f"✅ **Gmail監視システム復旧**\n{message}"
    try:
        requests.post(webhook_url, json={"content": content}, timeout=10)
    except Exception as e:
        print(f"[ERROR] Discordへの復旧通知送信失敗: {e}")


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
    send_discord_recovery(webhook_url, message)

    state["transient_network_issue_active"] = False
    state.pop("last_transient_error", None)
    state.pop("last_transient_error_at", None)
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
            print(
                f"[WARN] トークン更新で一時的な通信障害 (試行 {attempt}/{retries}): {e}. "
                f"{sleep_sec}秒後に再試行します。"
            )
            time.sleep(sleep_sec)

    return last_exc


# =========================
# Gmail API 関連 (強化版)
# =========================

def get_gmail_service(
    webhook_url: str = None,
    state: dict | None = None,
    state_file: Path | None = None,
):
    """
    Gmail API の service オブジェクトを返す
    トークンの自動更新を行い、失敗した場合はDiscordに通知する
    """
    creds = None

    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            print("[WARN] token.json の読み込みに失敗しました。再認証を試みます。")
            creds = None

    # トークンがない、または無効（期限切れ）の場合
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[INFO] トークンの期限が切れています。自動更新します...")
            refresh_error = refresh_with_retry(creds)
            if refresh_error is None:
                with TOKEN_PATH.open("w", encoding="utf-8") as token:
                    token.write(creds.to_json())
                print("[INFO] リフレッシュ後の token.json を保存しました。")
            elif is_transient_network_error(refresh_error):
                error_msg = (
                    "トークン更新時に一時的な通信障害が発生しました。"
                    "今回の実行はスキップし、次周期で自動復旧を待ちます。\n"
                    f"エラー: {refresh_error}"
                )
                print(f"[WARN] {error_msg}")
                if webhook_url:
                    send_discord_alert(webhook_url, f"⚠️ {error_msg}")
                if state is not None and state_file is not None:
                    mark_transient_network_issue(state, state_file, refresh_error)
                return None
            else:
                error_msg = (
                    "トークンの自動更新に失敗しました。再認証を試みます。\n"
                    f"エラー: {refresh_error}"
                )
                print(f"[ERROR] {error_msg}")
                # DiscordにSOSを送信しつつ、新規 OAuth を試す
                if webhook_url:
                    send_discord_alert(webhook_url, f"🚨 {error_msg}")
                creds = run_oauth_flow()
                if not creds:
                    return None
        else:
            # 初回ログイン、またはリフレッシュトークンがない場合
            # (通常、自動実行環境ではここは通らない想定だが、初回セットアップ用)
            print("[INFO] 新規ログインが必要です。ブラウザで認証してください。")
            creds = run_oauth_flow()
            if not creds:
                return None

    try:
        service = build("gmail", "v1", credentials=creds)
        return service
    except Exception as e:
        if is_transient_network_error(e):
            error_msg = (
                "Gmail API service 初期化時に一時的な通信障害が発生しました。"
                "次周期で再試行します。\n"
                f"エラー: {e}"
            )
            print(f"[WARN] {error_msg}")
            if webhook_url:
                send_discord_alert(webhook_url, error_msg)
            if state is not None and state_file is not None:
                mark_transient_network_issue(state, state_file, e)
            return None
        print(f"[ERROR] Service build failed: {e}")
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
):
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
        print("[INFO] Discord への送信に成功しました")
    except Exception as e:
        print(f"[ERROR] Discord への送信に失敗しました: {e}")


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
    print(f"[INFO] 前回処理した messageId: {last_message_id}")

    # ---- Gmail service 初期化 (更新処理付き) ----
    # Webhook URLを渡して、認証失敗時に通知できるようにする
    service = get_gmail_service(
        webhook_url=discord_webhook_url,
        state=state,
        state_file=state_file,
    )
    
    if service is None:
        print("[WARN] Gmail API の初期化に失敗したため、この周期をスキップします。")
        return

    # ---- メッセージ取得 ----
    query = "in:inbox"

    try:
        messages = list_recent_messages(
            service, query=query, max_results=max_messages
        )
    except HttpError as e:
        print(f"[ERROR] Gmail API 呼び出しでエラー: {e}")
        if discord_webhook_url:
            send_discord_alert(webhook_url=discord_webhook_url, message=f"Gmail API 呼び出しエラー: {e}")
        return
    except Exception as e:
        if is_transient_network_error(e):
            print(f"[WARN] Gmail API 取得時に一時的な通信障害: {e}")
            if discord_webhook_url:
                send_discord_alert(
                    webhook_url=discord_webhook_url,
                    message=f"Gmail API 取得で一時的な通信障害が発生しました。次周期で再試行します。\nエラー: {e}",
                )
            mark_transient_network_issue(state, state_file, e)
            return
        print(f"[ERROR] Gmail API 呼び出しで予期しないエラー: {e}")
        if discord_webhook_url:
            send_discord_alert(webhook_url=discord_webhook_url, message=f"Gmail API 予期しないエラー: {e}")
        return

    notify_recovery_if_needed(
        webhook_url=discord_webhook_url,
        state=state,
        state_file=state_file,
    )

    if not messages:
        print("[INFO] 対象メッセージなし")
        return

    newest_id_in_this_batch = messages[0]["id"]
    processed_any = False

    for msg_meta in messages:
        msg_id = msg_meta["id"]

        if last_message_id and msg_id == last_message_id:
            print(f"[INFO] 既に処理済みの境界 ({last_message_id}) に到達したので終了")
            break

        # ---- メッセージ詳細を取得 ----
        try:
            msg = get_message_detail(service, msg_id)
        except Exception as e:
            print(f"[WARN] メッセージ詳細取得失敗 (ID: {msg_id}): {e}")
            continue

        headers = msg.get("payload", {}).get("headers", [])
        snippet = msg.get("snippet", "")

        header_dict = {h["name"]: h["value"] for h in headers}
        subject_raw = header_dict.get("Subject", "(no subject)")
        from_raw = header_dict.get("From", "(unknown)")

        subject = decode_mime_words(subject_raw)
        from_decoded = decode_mime_words(from_raw)

        # ---- Amazon 判定 ----
        if not is_amazon_mail(from_decoded, amazon_pattern):
            continue

        if subject_pattern is not None:
            if not subject_pattern.search(subject):
                continue

        # ---- Discord に通知 ----
        url = build_gmail_message_url(msg_id)
        print(f"[INFO] Amazon メール検出: {subject} / {from_decoded}")
        send_discord_notification(
            webhook_url=discord_webhook_url,
            subject=subject,
            from_addr=extract_email_address(from_decoded),
            snippet=snippet,
            url=url,
        )
        processed_any = True

    # ---- state.json の更新 ----
    if newest_id_in_this_batch and newest_id_in_this_batch != last_message_id:
        state["last_message_id"] = newest_id_in_this_batch
        save_state(state_file, state)
        print(f"[INFO] state.json を更新: last_message_id = {newest_id_in_this_batch}")
    else:
        print("[INFO] last_message_id に変化はありませんでした")

    if not processed_any:
        print("[INFO] 新規に処理した Amazon メールはありませんでした")
    else:
        print("[INFO] Amazon メールの処理が完了しました")


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
    args = parser.parse_args()

    # ---- 設定読み込み ----
    if not CONFIG_PATH.exists():
        print(f"[ERROR] {CONFIG_PATH} が見つかりません。")
        sys.exit(1)

    config = load_config(CONFIG_PATH)
    discord_webhook_url = config.get("discord_webhook_url")

    if not discord_webhook_url:
        print("[ERROR] config.json に discord_webhook_url が設定されていません。")
        sys.exit(1)

    runtime = {
        "discord_webhook_url": discord_webhook_url,
        "amazon_pattern": config.get("amazon_from_pattern", r"amazon\\.co\\.jp"),
        "state_file": Path(config.get("state_file", BASE_DIR / "state.json")),
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

    print(f"[INFO] ループ監視モード開始。{poll_interval}秒ごとにチェックします。Ctrl+C で終了。")
    while True:
        time.sleep(poll_interval)
        try:
            run_once(runtime)
        except Exception as e:
            print(f"[ERROR] run_once 実行中に未処理例外: {e}")
            if discord_webhook_url:
                send_discord_alert(
                    webhook_url=discord_webhook_url,
                    message=f"未処理例外を検知しました。次周期で再試行します。\nエラー: {e}",
                )


if __name__ == "__main__":
    main()
