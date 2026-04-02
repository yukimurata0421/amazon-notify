import requests

from .config import LOGGER


def send_discord_alert(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False

    content = f"⚠️ **Gmail監視システム警告**\n{message}"
    try:
        response = requests.post(webhook_url, json={"content": content}, timeout=10)
        response.raise_for_status()
        return True
    except Exception as exc:
        LOGGER.error("DISCORD_ALERT_FAILED: %s", exc)
        return False


def send_discord_recovery(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False

    content = f"✅ **Gmail監視システム復旧**\n{message}"
    try:
        response = requests.post(webhook_url, json={"content": content}, timeout=10)
        response.raise_for_status()
        return True
    except Exception as exc:
        LOGGER.error("DISCORD_RECOVERY_FAILED: %s", exc)
        return False


def send_discord_notification(
    webhook_url: str,
    subject: str,
    from_addr: str,
    snippet: str,
    url: str,
) -> bool:
    content = (
        "📦 **Amazon 配達関連メールを検出しました**\n\n"
        f"**件名**: {subject}\n"
        f"**From**: {from_addr}\n"
        f"**プレビュー**: {snippet}\n"
        f"<{url}>"
    )

    try:
        response = requests.post(webhook_url, json={"content": content}, timeout=10)
        response.raise_for_status()
        LOGGER.info("DISCORD_NOTIFICATION_SENT")
        return True
    except Exception as exc:
        LOGGER.error("DISCORD_NOTIFICATION_FAILED: %s", exc)
        return False
