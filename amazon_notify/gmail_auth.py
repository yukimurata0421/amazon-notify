from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backoff import next_delay_seconds
from .config import LOGGER, RuntimePaths
from .domain import AuthStatus


def run_oauth_flow(
    *,
    runtime_paths: RuntimePaths,
    scopes: list[str],
    ensure_google_dependencies_fn: Callable[[], None],
    flow_factory: Any,
) -> Any | None:
    try:
        ensure_google_dependencies_fn()
        flow = flow_factory.from_client_secrets_file(
            str(runtime_paths.credentials),
            scopes,
        )
    except Exception as exc:
        LOGGER.error("OAUTH_PREPARE_FAILED: %s", exc)
        return None

    try:
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        LOGGER.warning("OAUTH_LOCAL_SERVER_FAILED: %s", exc)
        try:
            creds = flow.run_console()
        except Exception as fallback_exc:
            LOGGER.error("OAUTH_CONSOLE_FAILED: %s", fallback_exc)
            return None

    with runtime_paths.token.open("w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())
        LOGGER.info("TOKEN_SAVED: %s", runtime_paths.token)

    return creds


def refresh_with_retry(
    creds: Any,
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    request_factory: Callable[[], Any] | None = None,
    ensure_google_dependencies_fn: Callable[[], None],
    default_request_factory: Callable[[], Any],
    is_transient_network_error_fn: Callable[[Exception], bool],
    is_retryable_http_error_fn: Callable[[Exception], bool],
) -> Exception | None:
    if retries < 1:
        raise ValueError("retries must be >= 1")
    if request_factory is None:
        ensure_google_dependencies_fn()
        request_factory = default_request_factory

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            creds.refresh(request_factory())
            return None
        except Exception as exc:
            last_exc = exc
            is_transient = is_transient_network_error_fn(
                exc
            ) or is_retryable_http_error_fn(exc)
            if (not is_transient) or attempt == retries:
                break

            sleep_sec = next_delay_seconds(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            LOGGER.warning(
                "TOKEN_REFRESH_RETRY: attempt=%s/%s error=%s retry_in=%ss",
                attempt,
                retries,
                exc,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    return last_exc


def load_initial_credentials(
    *,
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
    runtime_paths: RuntimePaths,
    scopes: list[str],
    credentials_cls: Any,
    run_oauth_flow_fn: Callable[[RuntimePaths], Any | None],
    record_token_issue_and_maybe_alert_fn: Callable[
        [str | None, dict | None, Path | None, str, str], None
    ],
) -> tuple[Any | None, AuthStatus]:
    if not runtime_paths.token.exists():
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_MISSING_INTERACTIVE_AUTH_START")
            return run_oauth_flow_fn(runtime_paths), AuthStatus.TOKEN_MISSING

        reason = f"token.json が見つかりません: {runtime_paths.token}"
        LOGGER.error("TOKEN_MISSING: %s", reason)
        record_token_issue_and_maybe_alert_fn(
            webhook_url,
            state,
            state_file,
            reason,
            "token.json が存在しないため Gmail API に接続できません。"
            " `amazon-notify --reauth` で再認証してください。",
        )
        return None, AuthStatus.TOKEN_MISSING

    try:
        creds = credentials_cls.from_authorized_user_file(
            str(runtime_paths.token), scopes
        )
        if creds.valid:
            return creds, AuthStatus.TOKEN_VALID
        if creds.expired and creds.refresh_token:
            return creds, AuthStatus.TOKEN_EXPIRED_REFRESHABLE
        return creds, AuthStatus.INTERACTIVE_REAUTH_REQUIRED
    except Exception as exc:
        reason = f"token.json の読み込みに失敗: {exc}"
        LOGGER.error("TOKEN_INVALID: %s", reason)
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_INVALID_INTERACTIVE_AUTH_START")
            return run_oauth_flow_fn(runtime_paths), AuthStatus.TOKEN_CORRUPTED

        record_token_issue_and_maybe_alert_fn(
            webhook_url,
            state,
            state_file,
            reason,
            "token.json の読み込みに失敗しました。"
            " `amazon-notify --reauth` で再認証してください。",
        )
        return None, AuthStatus.TOKEN_CORRUPTED


def ensure_usable_credentials(
    *,
    creds: Any,
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
    runtime_paths: RuntimePaths,
    transient_alert_min_duration_seconds: float,
    transient_alert_cooldown_seconds: float,
    refresh_with_retry_fn: Callable[[Any], Exception | None],
    is_transient_network_error_fn: Callable[[Exception], bool],
    run_oauth_flow_fn: Callable[[RuntimePaths], Any | None],
    record_transient_issue_fn: Callable[
        [str | None, dict | None, Path | None, Exception | str, str, float, float], None
    ],
    record_token_issue_and_maybe_alert_fn: Callable[
        [str | None, dict | None, Path | None, str, str], None
    ],
    send_discord_alert_fn: Callable[[str, str], bool],
) -> tuple[Any | None, AuthStatus]:
    if creds.valid:
        return creds, AuthStatus.TOKEN_VALID

    if creds.expired and creds.refresh_token:
        LOGGER.info("TOKEN_REFRESH_START")
        refresh_error = refresh_with_retry_fn(creds)
        if refresh_error is None:
            with runtime_paths.token.open("w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            LOGGER.info("TOKEN_REFRESH_SUCCESS: %s", runtime_paths.token)
            return creds, AuthStatus.TOKEN_VALID

        if is_transient_network_error_fn(refresh_error):
            error_msg = (
                "トークン更新時に一時的な通信障害が発生しました。"
                "今回の実行はスキップし、次周期で自動復旧を待ちます。\n"
                f"エラー: {refresh_error}"
            )
            LOGGER.warning("TOKEN_REFRESH_TRANSIENT_FAILURE: %s", refresh_error)
            record_transient_issue_fn(
                webhook_url,
                state,
                state_file,
                refresh_error,
                error_msg,
                transient_alert_min_duration_seconds,
                transient_alert_cooldown_seconds,
            )
            return None, AuthStatus.REFRESH_TRANSIENT_FAILURE

        reason = f"トークンの自動更新に失敗: {refresh_error}"
        error_msg = (
            "トークンの自動更新に失敗しました。"
            " `amazon-notify --reauth` で再認証してください。\n"
            f"エラー: {refresh_error}"
        )
        LOGGER.error("TOKEN_REFRESH_FAILED: %s", reason)
        LOGGER.error("TOKEN_REFRESH_FATAL_FAILURE: %s", refresh_error)
        if allow_oauth_interactive:
            if webhook_url:
                send_discord_alert_fn(webhook_url, error_msg)
            return run_oauth_flow_fn(
                runtime_paths
            ), AuthStatus.REFRESH_PERMANENT_FAILURE

        record_token_issue_and_maybe_alert_fn(
            webhook_url,
            state,
            state_file,
            reason,
            error_msg,
        )
        return None, AuthStatus.REFRESH_PERMANENT_FAILURE

    reason = "token が無効で refresh_token も利用できません"
    LOGGER.error("TOKEN_INVALID_NO_REFRESH: %s", reason)
    if allow_oauth_interactive:
        LOGGER.info("TOKEN_INVALID_NO_REFRESH_INTERACTIVE_AUTH_START")
        return run_oauth_flow_fn(runtime_paths), AuthStatus.INTERACTIVE_REAUTH_REQUIRED

    record_token_issue_and_maybe_alert_fn(
        webhook_url,
        state,
        state_file,
        reason,
        "token が無効で自動更新できません。"
        " `amazon-notify --reauth` で再認証してください。",
    )
    return None, AuthStatus.INTERACTIVE_REAUTH_REQUIRED
