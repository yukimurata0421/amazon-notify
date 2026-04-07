from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from .config import LOGGER, load_state, save_state
from .time_utils import utc_now_iso

DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS = 600.0
DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS = 1800.0

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None  # type: ignore[assignment]


@contextmanager
def state_update_lock(state_file: Path):
    lock_path = state_file.parent / f".{state_file.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def sync_state_from_source(target: dict, source: dict) -> None:
    target.clear()
    target.update(source)


def clear_transient_issue_keys(state: dict) -> None:
    state["transient_network_issue_active"] = False
    state.pop("last_transient_error", None)
    state.pop("last_transient_error_at", None)
    state.pop("transient_network_issue_first_seen_at_epoch", None)
    state.pop("transient_network_issue_last_seen_at_epoch", None)
    state.pop("transient_network_issue_occurrences", None)
    state.pop("transient_network_issue_last_alert_at_epoch", None)
    state.pop("transient_network_issue_notified", None)


def should_send_transient_alert(
    state: dict,
    *,
    now_epoch: float,
    min_alert_duration_seconds: float,
    alert_cooldown_seconds: float,
) -> bool:
    first_seen = state.get("transient_network_issue_first_seen_at_epoch")
    if not isinstance(first_seen, (int, float)):
        return False
    if (now_epoch - float(first_seen)) < min_alert_duration_seconds:
        return False

    last_alert = state.get("transient_network_issue_last_alert_at_epoch")
    if isinstance(last_alert, (int, float)):
        if (now_epoch - float(last_alert)) < alert_cooldown_seconds:
            return False
    return True


def record_transient_issue(
    state: dict,
    state_file: Path,
    err: Exception | str,
    *,
    webhook_url: str | None = None,
    alert_message: str | None = None,
    min_alert_duration_seconds: float = DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS,
    alert_cooldown_seconds: float = DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS,
    send_discord_alert_fn: Callable[[str, str], bool],
) -> bool:
    if min_alert_duration_seconds < 0:
        LOGGER.warning(
            "TRANSIENT_ALERT_MIN_DURATION_CLAMPED: value=%s -> 0",
            min_alert_duration_seconds,
        )
        min_alert_duration_seconds = 0.0
    if alert_cooldown_seconds < 0:
        LOGGER.warning(
            "TRANSIENT_ALERT_COOLDOWN_CLAMPED: value=%s -> 0",
            alert_cooldown_seconds,
        )
        alert_cooldown_seconds = 0.0

    with state_update_lock(state_file):
        persisted_state = load_state(state_file)
        now_epoch = time.time()
        persisted_state["transient_network_issue_active"] = True
        persisted_state["last_transient_error"] = str(err)
        persisted_state["last_transient_error_at"] = utc_now_iso()
        first_seen = persisted_state.get("transient_network_issue_first_seen_at_epoch")
        if not isinstance(first_seen, (int, float)):
            first_seen = now_epoch
        persisted_state["transient_network_issue_first_seen_at_epoch"] = float(
            first_seen
        )
        persisted_state["transient_network_issue_last_seen_at_epoch"] = float(now_epoch)
        persisted_state["transient_network_issue_occurrences"] = (
            int(persisted_state.get("transient_network_issue_occurrences", 0)) + 1
        )

        sent_alert = False
        if (
            webhook_url
            and alert_message
            and should_send_transient_alert(
                persisted_state,
                now_epoch=now_epoch,
                min_alert_duration_seconds=min_alert_duration_seconds,
                alert_cooldown_seconds=alert_cooldown_seconds,
            )
        ):
            sent_alert = send_discord_alert_fn(webhook_url, alert_message)
            if sent_alert:
                persisted_state["transient_network_issue_notified"] = True
                persisted_state["transient_network_issue_last_alert_at_epoch"] = float(
                    now_epoch
                )

        save_state(state_file, persisted_state)
        sync_state_from_source(state, persisted_state)
        return sent_alert


def notify_recovery_if_needed(
    webhook_url: str,
    state: dict,
    state_file: Path,
    *,
    send_discord_recovery_fn: Callable[[str, str], bool],
) -> None:
    with state_update_lock(state_file):
        persisted_state = load_state(state_file)
        sync_state_from_source(state, persisted_state)

        if not persisted_state.get("transient_network_issue_active"):
            return
        if not persisted_state.get("transient_network_issue_notified"):
            clear_transient_issue_keys(persisted_state)
            save_state(state_file, persisted_state)
            sync_state_from_source(state, persisted_state)
            return

        message = (
            "一時的な通信障害から復旧しました。Gmail監視を再開しています。\n"
            f"前回障害時刻: {persisted_state.get('last_transient_error_at', '(unknown)')}\n"
            f"前回エラー: {persisted_state.get('last_transient_error', '(unknown)')}"
        )
        if not send_discord_recovery_fn(webhook_url, message):
            LOGGER.warning("TRANSIENT_RECOVERY_NOTIFICATION_SKIPPED")
            return

        clear_transient_issue_keys(persisted_state)
        save_state(state_file, persisted_state)
        sync_state_from_source(state, persisted_state)


def mark_token_issue(state: dict, state_file: Path, reason: str) -> bool:
    with state_update_lock(state_file):
        persisted_state = load_state(state_file)
        previous_active = bool(persisted_state.get("token_issue_active"))
        previous_reason = persisted_state.get("token_issue_reason")

        persisted_state["token_issue_active"] = True
        persisted_state["token_issue_reason"] = reason
        persisted_state["token_issue_at"] = utc_now_iso()
        save_state(state_file, persisted_state)
        sync_state_from_source(state, persisted_state)

        return (not previous_active) or (previous_reason != reason)


def notify_token_recovery_if_needed(
    webhook_url: str | None,
    state: dict,
    state_file: Path,
    *,
    send_discord_recovery_fn: Callable[[str, str], bool],
) -> None:
    with state_update_lock(state_file):
        persisted_state = load_state(state_file)
        sync_state_from_source(state, persisted_state)

        if not persisted_state.get("token_issue_active"):
            return

        if not webhook_url:
            LOGGER.warning("TOKEN_RECOVERY_NOTIFICATION_SKIPPED: missing_webhook")
            return

        message = (
            "token 問題から復旧しました。監視を再開しています。\n"
            f"前回障害時刻: {persisted_state.get('token_issue_at', '(unknown)')}\n"
            f"前回理由: {persisted_state.get('token_issue_reason', '(unknown)')}"
        )
        if not send_discord_recovery_fn(webhook_url, message):
            LOGGER.warning("TOKEN_RECOVERY_NOTIFICATION_SKIPPED")
            return

        LOGGER.info("TOKEN_RECOVERED: %s", message.replace("\n", " | "))
        persisted_state["token_issue_active"] = False
        persisted_state.pop("token_issue_reason", None)
        persisted_state.pop("token_issue_at", None)
        save_state(state_file, persisted_state)
        sync_state_from_source(state, persisted_state)


def record_token_issue_and_maybe_alert(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    reason: str,
    alert_message: str,
    *,
    send_discord_alert_fn: Callable[[str, str], bool],
) -> None:
    if state is None or state_file is None:
        return
    should_alert = mark_token_issue(state, state_file, reason)
    if webhook_url and should_alert:
        send_discord_alert_fn(webhook_url, alert_message)


def record_transient_issue_or_alert(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    error: Exception | str,
    alert_message: str,
    min_alert_duration_seconds: float,
    alert_cooldown_seconds: float,
    *,
    send_discord_alert_fn: Callable[[str, str], bool],
) -> None:
    if state is None or state_file is None:
        if webhook_url:
            send_discord_alert_fn(webhook_url, alert_message)
        return
    record_transient_issue(
        state,
        state_file,
        error,
        webhook_url=webhook_url,
        alert_message=alert_message,
        min_alert_duration_seconds=min_alert_duration_seconds,
        alert_cooldown_seconds=alert_cooldown_seconds,
        send_discord_alert_fn=send_discord_alert_fn,
    )
