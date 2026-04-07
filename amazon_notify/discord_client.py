from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import requests

from .backoff import next_delay_seconds, parse_retry_after_seconds
from .config import LOGGER, save_state

DEFAULT_DISCORD_MAX_ATTEMPTS = 4
DEFAULT_DISCORD_BASE_DELAY_SECONDS = 1.0
DEFAULT_DISCORD_MAX_DELAY_SECONDS = 30.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_DEDUPE_SCHEMA_VERSION = 1
_DEDUPE_LOCK_FILENAME = ".discord_dedupe_state.lock"
_DEDUPE_MAX_RETENTION_SECONDS = 31 * 24 * 60 * 60
_DEDUPE_INFLIGHT_TTL_SECONDS = 120.0
_DEDUPE_WINDOW_SECONDS = {
    "alert": 900.0,
    "recovery": 900.0,
    "test": 60.0,
    "notification": 7 * 24 * 60 * 60.0,
}

_NON_RETRYABLE_REQUEST_EXCEPTIONS = (
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
    requests.exceptions.URLRequired,
)

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None  # type: ignore[assignment]


def has_dedupe_file_lock_support() -> bool:
    return fcntl is not None


def _ensure_dedupe_lock_supported() -> None:
    if has_dedupe_file_lock_support():
        return
    LOGGER.error(
        "DISCORD_DEDUPE_LOCK_UNSUPPORTED: fcntl が利用できない環境です。"
        " dedupe lock は利用できません。Linux 単一ホスト運用を推奨します。"
    )
    raise OSError("fcntl is unavailable; discord dedupe lock is not supported on this platform")


@contextmanager
def _discord_dedupe_lock(state_path: Path):
    _ensure_dedupe_lock_supported()
    lock_path = state_path.parent / _DEDUPE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        assert fcntl is not None
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _read_dedupe_entries(state_path: Path) -> dict[str, dict[str, float | str]]:
    if not state_path.exists():
        return {}

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("DISCORD_DEDUPE_STATE_CORRUPTED: path=%s", state_path)
        return {}

    raw_entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
    if not isinstance(raw_entries, dict):
        return {}

    entries: dict[str, dict[str, float | str]] = {}
    for key, raw in raw_entries.items():
        if not isinstance(key, str):
            continue

        entry: dict[str, float | str] = {}
        if isinstance(raw, (int, float)):
            entry["last_sent_at"] = float(raw)
            entries[key] = entry
            continue
        if not isinstance(raw, dict):
            continue

        last_sent_at = raw.get("last_sent_at")
        if isinstance(last_sent_at, (int, float)):
            entry["last_sent_at"] = float(last_sent_at)

        inflight_until_obj: object = raw.get("inflight_until")
        has_valid_inflight_until = False
        if isinstance(inflight_until_obj, (int, float)):
            has_valid_inflight_until = True
            entry["inflight_until"] = float(inflight_until_obj)

        # inflight_owner は inflight_until とセットでのみ意味を持つ。
        inflight_owner = raw.get("inflight_owner")
        if has_valid_inflight_until and isinstance(inflight_owner, str) and inflight_owner:
            entry["inflight_owner"] = inflight_owner

        if not entry:
            # 型不整合だけの壊れた entry はこの段階で破棄する。
            continue
        entries[key] = entry
    return entries


def _write_dedupe_entries(state_path: Path, entries: dict[str, dict[str, float | str]]) -> None:
    payload = {
        "schema_version": _DEDUPE_SCHEMA_VERSION,
        "entries": entries,
    }
    save_state(state_path, payload)


def _prune_dedupe_entries(
    entries: dict[str, dict[str, float | str]],
    *,
    now_epoch: float,
) -> bool:
    changed = False
    for key in list(entries.keys()):
        entry = dict(entries[key])

        inflight_until = entry.get("inflight_until")
        if isinstance(inflight_until, (int, float)) and float(inflight_until) <= now_epoch:
            entry.pop("inflight_until", None)
            entry.pop("inflight_owner", None)
            changed = True

        # owner だけ残る壊れた entry は明示的に掃除する。
        if "inflight_owner" in entry and "inflight_until" not in entry:
            entry.pop("inflight_owner", None)
            changed = True

        last_sent_at = entry.get("last_sent_at")
        if isinstance(last_sent_at, (int, float)):
            if (now_epoch - float(last_sent_at)) > _DEDUPE_MAX_RETENTION_SECONDS and "inflight_until" not in entry:
                entries.pop(key, None)
                changed = True
                continue
        elif "inflight_until" not in entry:
            entries.pop(key, None)
            changed = True
            continue

        entries[key] = entry
    return changed


def _build_dedupe_key(notification_kind: str, content: str) -> str:
    digest = sha256(content.encode("utf-8")).hexdigest()
    return f"{notification_kind}:{digest}"


def _reserve_dedupe_claim(
    *,
    notification_kind: str,
    content: str,
    dedupe_state_path: Path | None,
    dedupe_window_seconds: float,
) -> tuple[bool, Path | None, str | None, str | None]:
    if dedupe_window_seconds <= 0 or dedupe_state_path is None:
        return True, None, None, None

    state_path = dedupe_state_path
    dedupe_key = _build_dedupe_key(notification_kind, content)
    owner = uuid.uuid4().hex
    now_epoch = time.time()

    try:
        with _discord_dedupe_lock(state_path):
            entries = _read_dedupe_entries(state_path)
            changed = _prune_dedupe_entries(entries, now_epoch=now_epoch)
            entry = entries.get(dedupe_key, {})

            last_sent_at = entry.get("last_sent_at")
            if isinstance(last_sent_at, (int, float)):
                elapsed = now_epoch - float(last_sent_at)
                if elapsed < dedupe_window_seconds:
                    LOGGER.warning(
                        "DISCORD_DEDUPE_SUPPRESSED: kind=%s elapsed=%.1fs window=%.1fs",
                        notification_kind,
                        elapsed,
                        dedupe_window_seconds,
                    )
                    if changed:
                        _write_dedupe_entries(state_path, entries)
                    return False, None, None, None

            inflight_until = entry.get("inflight_until")
            if isinstance(inflight_until, (int, float)) and float(inflight_until) > now_epoch:
                LOGGER.warning(
                    "DISCORD_DEDUPE_SUPPRESSED_INFLIGHT: kind=%s remaining=%.1fs",
                    notification_kind,
                    float(inflight_until) - now_epoch,
                )
                if changed:
                    _write_dedupe_entries(state_path, entries)
                return False, None, None, None

            next_entry: dict[str, float | str] = {}
            if isinstance(last_sent_at, (int, float)):
                next_entry["last_sent_at"] = float(last_sent_at)
            next_entry["inflight_owner"] = owner
            next_entry["inflight_until"] = now_epoch + _DEDUPE_INFLIGHT_TTL_SECONDS
            entries[dedupe_key] = next_entry
            _write_dedupe_entries(state_path, entries)
    except OSError as exc:
        LOGGER.warning("DISCORD_DEDUPE_BYPASS: kind=%s error=%s", notification_kind, exc)
        return True, None, None, None

    return True, state_path, dedupe_key, owner


def _finalize_dedupe_claim(
    *,
    state_path: Path | None,
    dedupe_key: str | None,
    owner: str | None,
    sent: bool,
) -> None:
    if state_path is None or dedupe_key is None or owner is None:
        return

    now_epoch = time.time()
    try:
        with _discord_dedupe_lock(state_path):
            entries = _read_dedupe_entries(state_path)
            _prune_dedupe_entries(entries, now_epoch=now_epoch)
            entry = entries.get(dedupe_key)
            if not isinstance(entry, dict):
                return
            if entry.get("inflight_owner") != owner:
                return

            entry.pop("inflight_owner", None)
            entry.pop("inflight_until", None)
            if sent:
                entry["last_sent_at"] = now_epoch
            elif "last_sent_at" not in entry:
                entries.pop(dedupe_key, None)
                _write_dedupe_entries(state_path, entries)
                return

            entries[dedupe_key] = entry
            _write_dedupe_entries(state_path, entries)
    except OSError as exc:
        LOGGER.warning("DISCORD_DEDUPE_FINALIZE_FAILED: key=%s error=%s", dedupe_key, exc)


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


def _post_webhook_with_dedupe(
    *,
    webhook_url: str,
    content: str,
    notification_kind: str,
    dedupe_state_path: Path | None = None,
    dedupe_window_seconds: float,
    max_attempts: int = DEFAULT_DISCORD_MAX_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_DISCORD_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_DISCORD_MAX_DELAY_SECONDS,
) -> bool:
    allowed, state_path, dedupe_key, owner = _reserve_dedupe_claim(
        notification_kind=notification_kind,
        content=content,
        dedupe_state_path=dedupe_state_path,
        dedupe_window_seconds=dedupe_window_seconds,
    )
    if not allowed:
        return True

    sent = _post_webhook(
        webhook_url,
        content,
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    _finalize_dedupe_claim(
        state_path=state_path,
        dedupe_key=dedupe_key,
        owner=owner,
        sent=sent,
    )
    return sent


def send_discord_alert(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None = None,
) -> bool:
    if not webhook_url:
        return False

    content = f"⚠️ **Gmail監視システム警告**\n{message}"
    return _post_webhook_with_dedupe(
        webhook_url=webhook_url,
        content=content,
        notification_kind="alert",
        dedupe_state_path=dedupe_state_path,
        dedupe_window_seconds=_DEDUPE_WINDOW_SECONDS["alert"],
    )


def send_discord_recovery(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None = None,
) -> bool:
    if not webhook_url:
        return False

    content = f"✅ **Gmail監視システム復旧**\n{message}"
    return _post_webhook_with_dedupe(
        webhook_url=webhook_url,
        content=content,
        notification_kind="recovery",
        dedupe_state_path=dedupe_state_path,
        dedupe_window_seconds=_DEDUPE_WINDOW_SECONDS["recovery"],
    )


def send_discord_test(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None = None,
) -> bool:
    if not webhook_url:
        return False

    content = f"🧪 **Amazon Notify テスト通知**\n{message}"
    return _post_webhook_with_dedupe(
        webhook_url=webhook_url,
        content=content,
        notification_kind="test",
        dedupe_state_path=dedupe_state_path,
        dedupe_window_seconds=_DEDUPE_WINDOW_SECONDS["test"],
    )


def send_discord_notification(
    webhook_url: str,
    subject: str,
    from_addr: str,
    snippet: str,
    url: str,
    *,
    dedupe_state_path: Path | None = None,
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

    sent = _post_webhook_with_dedupe(
        webhook_url=webhook_url,
        content=content,
        notification_kind="notification",
        dedupe_state_path=dedupe_state_path,
        dedupe_window_seconds=_DEDUPE_WINDOW_SECONDS["notification"],
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    if sent:
        LOGGER.info("DISCORD_NOTIFICATION_SENT")
        return True
    LOGGER.error("DISCORD_NOTIFICATION_FAILED")
    return False
