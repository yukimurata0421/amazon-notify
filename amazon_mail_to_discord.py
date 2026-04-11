#!/usr/bin/env python3
"""
Gmail(IMAP) を1分ごとにチェックして、
Amazon.co.jp からの「発送 / 配達 / お届け」メールだけ
Discord に通知するスクリプト。

・Raspberry Pi 5 で動作想定
・Gmail へのログインはアプリパスワードを使用
"""

import email
import imaplib
import os
import re
import time
from email.header import decode_header

import requests

# ======== 設定（環境変数） ========

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# 件名に含まれていたら通知するキーワード
# 例: export AMAZON_KEYWORDS="発送,配達,お届け"
KEYWORDS_ENV = os.environ.get("AMAZON_KEYWORDS", "発送,配達,お届け")
SUBJECT_KEYWORDS = [k.strip() for k in KEYWORDS_ENV.split(",") if k.strip()]

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# このプロセス起動中に処理したメールUID
processed_uids: set[str] = set()


# ======== ユーティリティ関数 ========


def decode_mime_words(value: str) -> str:
    """MIMEエンコードされた文字列をデコードする"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded += part.decode(enc or "utf-8", errors="ignore")
        else:
            decoded += part
    return decoded


def get_text_body(msg: email.message.Message) -> str:
    """メール本文 (text/plain) を抽出する。なければ空文字。"""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    return part.get_payload(decode=True).decode(
                        charset, errors="ignore"
                    )
                except Exception:
                    continue
    else:
        if msg.get_content_type() == "text/plain":
            try:
                charset = msg.get_content_charset() or "utf-8"
                return msg.get_payload(decode=True).decode(charset, errors="ignore")
            except Exception:
                return ""
    return ""


def send_to_discord(subject: str, from_addr: str, date: str, body: str):
    """Discord Webhook に通知を送る"""

    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL が未設定です。")
        return

    # 状態に応じて絵文字とタイトル変更
    if "配達済" in subject or "配達完了" in subject:
        emoji = "📬"
        title = "**お届け完了！**"
    elif "配達中" in subject:
        emoji = "🚚"
        title = "**配送中です！**"
    elif "発送" in subject:
        emoji = "📦"
        title = "**発送されました！**"
    else:
        emoji = "📦"
        title = "**Amazon通知**"

    content = (
        f"{emoji} {title}\n"
        f"**件名:** {subject}\n"
        f"**From:** {from_addr}\n"
        f"**Date:** {date}\n\n"
        f"**本文抜粋:**\n{body}"
    )

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
        if resp.status_code >= 400:
            print("Discord送信エラー:", resp.status_code, resp.text)
    except Exception as e:
        print("Discord送信中にエラー:", e)


# ======== Gmail チェック本体 ========


def check_gmail_once():
    """未読の Amazon メールを探し、Discord に通知する"""

    global processed_uids

    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("GMAIL_ADDRESS または GMAIL_APP_PASSWORD が設定されていません。")
        return

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

        # INBOX を選択
        status, _ = mail.select("INBOX")
        if status != "OK":
            print("INBOX の選択に失敗しました。")
            mail.logout()
            return

        print("Gmail: INBOX を選択 → 未読メールを検索します...")

        # 未読メールのみ検索
        status, data = mail.uid("search", None, "UNSEEN")
        if status != "OK":
            print("未読メール検索に失敗しました。status =", status, "data =", data)
            mail.logout()
            return

        uid_list = data[0].split()  # [b'123', b'124', ...]
        total_unseen = len(uid_list)
        print(f"Gmail: 未読メール UID 数 = {total_unseen}")

        if not uid_list:
            print("Gmail: 未読メールはありません。")
            mail.logout()
            return

        # 未読が多すぎるので、最新だけに絞る
        MAX_CHECK = 50
        if total_unseen > MAX_CHECK:
            uid_list = uid_list[-MAX_CHECK:]
            print(f"Gmail: 最新 {MAX_CHECK} 通のみチェックします。")

        for uid in uid_list:
            uid_str = uid.decode("utf-8")

            # すでに処理済みならスキップ
            if uid_str in processed_uids:
                continue

            # メール取得
            status, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if status != "OK":
                print(f"メール取得に失敗しました UID={uid_str}")
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # 件名
            subject_raw = msg.get("Subject", "")
            subject = decode_mime_words(subject_raw)

            # --- 差出人（メールアドレスだけ抽出）---
            from_raw = msg.get("From", "")
            from_decoded = decode_mime_words(from_raw)

            # 正規表現でメールアドレスだけ抜き出す
            match = re.search(r"[\w\.-]+@[\w\.-]+", from_decoded)
            from_email = match.group(0).lower() if match else ""

            # Amazon 以外はスキップ
            if not from_email.endswith("@amazon.co.jp"):
                processed_uids.add(uid_str)
                continue

            # 件名キーワード判定
            if not any(keyword in subject for keyword in SUBJECT_KEYWORDS):
                processed_uids.add(uid_str)
                continue

            # 本文の抜粋
            body = get_text_body(msg)
            body_snippet = body.replace("\r", "").strip()
            if len(body_snippet) > 200:
                body_snippet = body_snippet[:200] + "..."

            print(
                f"★ Amazon メール検出: UID={uid_str} | 件名={subject} | From={from_email}"
            )

            # Discord に通知
            send_to_discord(subject, from_email, msg.get("Date", ""), body_snippet)

            # 処理済みに追加（再通知防止）
            processed_uids.add(uid_str)

        mail.logout()

    except imaplib.IMAP4.error as e:
        print("IMAPエラー:", e)
    except Exception as e:
        print("check_gmail_once 中に例外:", e)


# ======== メインループ ========


def main():
    print("Amazon.co.jp 配達メール監視を開始（1分ごとにチェック）")
    print("件名キーワード:", SUBJECT_KEYWORDS)
    print("Amazon（@amazon.co.jp）以外は通知しません。\n")

    while True:
        check_gmail_once()
        time.sleep(60)


if __name__ == "__main__":
    main()
