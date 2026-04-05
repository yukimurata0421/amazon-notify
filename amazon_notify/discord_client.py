from __future__ import annotations

import time

import requests

from .backoff import next_delay_seconds, parse_retry_after_seconds
from .config import LOGGER

DEFAULT_DISCORD_MAX_ATTEMPTS = 4
DEFAULT_DISCORD_BASE_DELAY_SECONDS = 1.0
DEFAULT_DISCORD_MAX_DELAY_SECONDS = 30.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_NON_RETRYABLE_REQUEST_EXCEPTIONS = (
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.URLRequired,
)


def _should_retry_request_exception(exc: Exception) -> bool:
    return isinstance(exc, requests.exceptions.RequestException) and not isinstance(
        exc, _NON_RETRYABLE_REQUEST_EXCEPTIONS
    )


def _post_webhook(
    webhook_url: str,
    content: str,
    *,
    max_attempts: int = DEFAULT_DISCORD_MAX_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_DISCORD_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_DISCORD_MAX_DELAY_SECONDS,
) -> bool:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(webhook_url, json={"content": content}, timeout=10)
        except Exception as exc:
            if _should_retry_request_exception(exc) and attempt < max_attempts:
                delay = next_delay_seconds(
                    attempt,
                    base_delay=base_delay_seconds,
                    max_delay=max_delay_seconds,
                )
                LOGGER.warning(
                    "DISCORD_POST_RETRY_EXCEPTION: attempt=%s/%s retry_in=%.2fs error=%s",
                    attempt,
                    max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue
            LOGGER.error("DISCORD_POST_FAILED: %s", exc)
            return False

        status_code = getattr(response, "status_code", None)
        if status_code is None:
            try:
                response.raise_for_status()
                return True
            except Exception as exc:
                LOGGER.error("DISCORD_POST_FAILED: %s", exc)
                return False

        if 200 <= status_code < 300:
            return True

        if status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
            retry_after = parse_retry_after_seconds(
                getattr(response, "headers", {}).get("Retry-After")
            )
            delay = retry_after if retry_after is not None else next_delay_seconds(
                attempt,
                base_delay=base_delay_seconds,
                max_delay=max_delay_seconds,
            )
            LOGGER.warning(
                "DISCORD_POST_RETRY_STATUS: attempt=%s/%s status=%s retry_in=%.2fs",
                attempt,
                max_attempts,
                status_code,
                delay,
            )
            time.sleep(delay)
            continue

        LOGGER.error(
            "DISCORD_POST_FAILED: status=%s body=%s",
            status_code,
            getattr(response, "text", ""),
        )
        return False

    return False


def send_discord_alert(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False

    content = f"⚠️ **Gmail監視システム警告**\n{message}"
    return _post_webhook(webhook_url, content)


def send_discord_recovery(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False

    content = f"✅ **Gmail監視システム復旧**\n{message}"
    return _post_webhook(webhook_url, content)


def send_discord_test(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False

    content = f"🧪 **Amazon Notify テスト通知**\n{message}"
    return _post_webhook(webhook_url, content)


def send_discord_notification(
    webhook_url: str,
    subject: str,
    from_addr: str,
    snippet: str,
    url: str,
    *,
    max_attempts: int = DEFAULT_DISCORD_MAX_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_DISCORD_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_DISCORD_MAX_DELAY_SECONDS,
) -> bool:
    content = (
        "📦 **Amazon 配達関連メールを検出しました**\n\n"
        f"**件名**: {subject}\n"
        f"**From**: {from_addr}\n"
        f"**プレビュー**: {snippet}\n"
        f"<{url}>"
    )

    sent = _post_webhook(
        webhook_url,
        content,
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    if sent:
        LOGGER.info("DISCORD_NOTIFICATION_SENT")
        return True
    LOGGER.error("DISCORD_NOTIFICATION_FAILED")
    return False
