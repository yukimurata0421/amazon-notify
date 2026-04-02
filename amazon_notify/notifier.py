from .config import LOGGER, load_state, save_state
from .discord_client import send_discord_alert, send_discord_notification
from .gmail_client import (
    HttpError,
    get_gmail_service,
    get_message_detail,
    is_transient_network_error,
    list_recent_messages,
    mark_transient_network_issue,
    notify_recovery_if_needed,
)
from .text import build_gmail_message_url, decode_mime_words, extract_email_address, is_amazon_mail


def run_once(runtime: dict) -> None:
    discord_webhook_url = runtime["discord_webhook_url"]
    amazon_pattern = runtime["amazon_pattern"]
    state_file = runtime["state_file"]
    max_messages = runtime["max_messages"]
    subject_pattern = runtime["subject_pattern"]
    dry_run = bool(runtime.get("dry_run", False))

    state = load_state(state_file)
    last_message_id = state.get("last_message_id")
    LOGGER.info("RUN_ONCE_START: last_message_id=%s dry_run=%s", last_message_id, dry_run)

    service = get_gmail_service(
        webhook_url=None if dry_run else discord_webhook_url,
        state=None if dry_run else state,
        state_file=None if dry_run else state_file,
    )
    if service is None:
        LOGGER.warning("RUN_ONCE_SKIPPED: gmail_service_unavailable")
        return

    try:
        messages = list_recent_messages(service, query="in:inbox", max_results=max_messages)
    except HttpError as exc:
        LOGGER.error("GMAIL_API_HTTP_ERROR: %s", exc)
        if discord_webhook_url and not dry_run:
            send_discord_alert(discord_webhook_url, f"Gmail API 呼び出しエラー: {exc}")
        return
    except Exception as exc:
        if is_transient_network_error(exc):
            LOGGER.warning("GMAIL_API_TRANSIENT_ERROR: %s", exc)
            if discord_webhook_url and not dry_run:
                send_discord_alert(
                    discord_webhook_url,
                    f"Gmail API 取得で一時的な通信障害が発生しました。次周期で再試行します。\nエラー: {exc}",
                )
            if not dry_run:
                mark_transient_network_issue(state, state_file, exc)
            return
        LOGGER.error("GMAIL_API_UNEXPECTED_ERROR: %s", exc)
        if discord_webhook_url and not dry_run:
            send_discord_alert(discord_webhook_url, f"Gmail API 予期しないエラー: {exc}")
        return

    if not dry_run:
        notify_recovery_if_needed(discord_webhook_url, state, state_file)

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
        try:
            msg = get_message_detail(service, msg_id)
        except Exception as exc:
            LOGGER.warning("MESSAGE_DETAIL_FETCH_FAILED: id=%s error=%s", msg_id, exc)
            if discord_webhook_url and not dry_run:
                send_discord_alert(
                    discord_webhook_url,
                    f"メッセージ詳細の取得に失敗しました。次周期で再試行します。\nmessage_id: {msg_id}\nエラー: {exc}",
                )
            break

        headers = msg.get("payload", {}).get("headers", [])
        header_dict = {header["name"]: header["value"] for header in headers}
        subject = decode_mime_words(header_dict.get("Subject", "(no subject)"))
        from_decoded = decode_mime_words(header_dict.get("From", "(unknown)"))

        should_notify = is_amazon_mail(from_decoded, amazon_pattern)
        if should_notify and subject_pattern is not None:
            should_notify = subject_pattern.search(subject) is not None

        if should_notify:
            LOGGER.info("AMAZON_MAIL_DETECTED: id=%s subject=%s from=%s", msg_id, subject, from_decoded)
            if dry_run:
                LOGGER.info(
                    "DRY_RUN_NOTIFICATION: id=%s subject=%s from=%s",
                    msg_id,
                    subject,
                    extract_email_address(from_decoded),
                )
                sent = True
            else:
                sent = send_discord_notification(
                    webhook_url=discord_webhook_url,
                    subject=subject,
                    from_addr=extract_email_address(from_decoded),
                    snippet=msg.get("snippet", ""),
                    url=build_gmail_message_url(msg_id),
                )
            if not sent:
                if discord_webhook_url and not dry_run:
                    send_discord_alert(
                        discord_webhook_url,
                        "Amazon メールの Discord 通知に失敗しました。"
                        " state は更新していないため、次周期で再試行します。\n"
                        f"message_id: {msg_id}",
                    )
                break
            processed_any = True

        last_processed_id = msg_id

    if dry_run:
        LOGGER.info("DRY_RUN_STATE_UNCHANGED")
    elif last_processed_id and last_processed_id != last_message_id:
        state["last_message_id"] = last_processed_id
        save_state(state_file, state)
        LOGGER.info("STATE_UPDATED: last_message_id=%s", last_processed_id)
    else:
        LOGGER.info("STATE_UNCHANGED")

    if not processed_any:
        LOGGER.info("RUN_ONCE_COMPLETE: amazon_notifications=0")
    else:
        LOGGER.info("RUN_ONCE_COMPLETE: amazon_notifications>=1")
