import socket
import time
from typing import Any

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GOOGLE_IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:
    class MissingCredentials:
        valid = False
        expired = False
        refresh_token = None

        @classmethod
        def from_authorized_user_file(cls, *_args, **_kwargs):
            raise exc

        def refresh(self, *_args, **_kwargs):
            raise exc

        def to_json(self) -> str:
            raise exc

    class MissingHttpError(Exception):
        """Fallback error type used when googleapiclient is unavailable."""

    Request = None  # type: ignore[assignment]
    Credentials = MissingCredentials  # type: ignore[assignment]
    InstalledAppFlow = None  # type: ignore[assignment]
    build = None  # type: ignore[assignment]
    HttpError = MissingHttpError  # type: ignore[assignment]
    GOOGLE_IMPORT_ERROR = exc

from . import config as app_config
from .config import LOGGER, save_state
from .discord_client import send_discord_alert, send_discord_recovery

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def ensure_google_dependencies() -> None:
    if GOOGLE_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "Google API libraries are missing. Install runtime deps with: "
            "`pip install .`"
        ) from GOOGLE_IMPORT_ERROR


def run_oauth_flow() -> Credentials | None:
    try:
        ensure_google_dependencies()
        flow = InstalledAppFlow.from_client_secrets_file(
            str(app_config.CREDENTIALS_PATH),
            SCOPES,
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

    with app_config.TOKEN_PATH.open("w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())
        LOGGER.info("TOKEN_SAVED: %s", app_config.TOKEN_PATH)

    return creds


def mark_transient_network_issue(state: dict, state_file, err: Exception | str) -> None:
    state["transient_network_issue_active"] = True
    state["last_transient_error"] = str(err)
    state["last_transient_error_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_file, state)


def notify_recovery_if_needed(webhook_url: str, state: dict, state_file) -> None:
    if not state.get("transient_network_issue_active"):
        return

    message = (
        "一時的な通信障害から復旧しました。Gmail監視を再開しています。\n"
        f"前回障害時刻: {state.get('last_transient_error_at', '(unknown)')}\n"
        f"前回エラー: {state.get('last_transient_error', '(unknown)')}"
    )
    if not send_discord_recovery(webhook_url, message):
        LOGGER.warning("TRANSIENT_RECOVERY_NOTIFICATION_SKIPPED")
        return

    state["transient_network_issue_active"] = False
    state.pop("last_transient_error", None)
    state.pop("last_transient_error_at", None)
    save_state(state_file, state)


def mark_token_issue(state: dict, state_file, reason: str) -> bool:
    previous_active = bool(state.get("token_issue_active"))
    previous_reason = state.get("token_issue_reason")

    state["token_issue_active"] = True
    state["token_issue_reason"] = reason
    state["token_issue_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_file, state)

    return (not previous_active) or (previous_reason != reason)


def notify_token_recovery_if_needed(webhook_url: str | None, state: dict, state_file) -> None:
    if not state.get("token_issue_active"):
        return

    message = (
        "token 問題から復旧しました。監視を再開しています。\n"
        f"前回障害時刻: {state.get('token_issue_at', '(unknown)')}\n"
        f"前回理由: {state.get('token_issue_reason', '(unknown)')}"
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
    visited: set[int] = set()
    while current and id(current) not in visited:
        visited.add(id(current))
        text = f"{type(current).__name__}: {current}".lower()
        if any(keyword in text for keyword in transient_keywords):
            return True
        current = current.__cause__ or current.__context__

    return False


def refresh_with_retry(creds: Credentials, retries: int = 3, base_delay: int = 2) -> Exception | None:
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            creds.refresh(Request())
            return None
        except Exception as exc:
            last_exc = exc
            if not is_transient_network_error(exc) or attempt == retries:
                return last_exc

            sleep_sec = base_delay * attempt
            LOGGER.warning(
                "TOKEN_REFRESH_RETRY: attempt=%s/%s error=%s retry_in=%ss",
                attempt,
                retries,
                exc,
                sleep_sec,
            )
            time.sleep(sleep_sec)

    return last_exc


def get_gmail_service(
    webhook_url: str | None = None,
    state: dict | None = None,
    state_file=None,
    allow_oauth_interactive: bool = False,
):
    creds = None
    try:
        ensure_google_dependencies()
    except ModuleNotFoundError as exc:
        LOGGER.error("DEPENDENCY_MISSING: %s", exc)
        return None

    if not app_config.TOKEN_PATH.exists():
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_MISSING_INTERACTIVE_AUTH_START")
            creds = run_oauth_flow()
            if not creds:
                return None
        else:
            reason = f"token.json が見つかりません: {app_config.TOKEN_PATH}"
            LOGGER.error("TOKEN_MISSING: %s", reason)
            if webhook_url and state is not None and state_file is not None:
                should_alert = mark_token_issue(state, state_file, reason)
                if should_alert:
                    send_discord_alert(
                        webhook_url,
                        "token.json が存在しないため Gmail API に接続できません。"
                        " `amazon-notify --reauth` で再認証してください。",
                    )
            return None
    else:
        try:
            creds = Credentials.from_authorized_user_file(str(app_config.TOKEN_PATH), SCOPES)
        except Exception as exc:
            reason = f"token.json の読み込みに失敗: {exc}"
            LOGGER.error("TOKEN_INVALID: %s", reason)
            if allow_oauth_interactive:
                LOGGER.info("TOKEN_INVALID_INTERACTIVE_AUTH_START")
                creds = run_oauth_flow()
            else:
                if webhook_url and state is not None and state_file is not None:
                    should_alert = mark_token_issue(state, state_file, reason)
                    if should_alert:
                        send_discord_alert(
                            webhook_url,
                            "token.json の読み込みに失敗しました。"
                            " `amazon-notify --reauth` で再認証してください。",
                        )
                return None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            LOGGER.info("TOKEN_REFRESH_START")
            refresh_error = refresh_with_retry(creds)
            if refresh_error is None:
                with app_config.TOKEN_PATH.open("w", encoding="utf-8") as token_file:
                    token_file.write(creds.to_json())
                LOGGER.info("TOKEN_REFRESH_SUCCESS: %s", app_config.TOKEN_PATH)
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
                error_msg = (
                    "トークンの自動更新に失敗しました。"
                    " `amazon-notify --reauth` で再認証してください。\n"
                    f"エラー: {refresh_error}"
                )
                LOGGER.error("TOKEN_REFRESH_FAILED: %s", reason)
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
                            " `amazon-notify --reauth` で再認証してください。",
                        )
                return None

    try:
        service = build("gmail", "v1", credentials=creds)
        if state is not None and state_file is not None:
            notify_token_recovery_if_needed(webhook_url, state, state_file)
        return service
    except Exception as exc:
        if is_transient_network_error(exc):
            error_msg = (
                "Gmail API service 初期化時に一時的な通信障害が発生しました。"
                "次周期で再試行します。\n"
                f"エラー: {exc}"
            )
            LOGGER.warning("GMAIL_SERVICE_TRANSIENT_FAILURE: %s", exc)
            if webhook_url:
                send_discord_alert(webhook_url, error_msg)
            if state is not None and state_file is not None:
                mark_transient_network_issue(state, state_file, exc)
            return None
        LOGGER.error("GMAIL_SERVICE_BUILD_FAILED: %s", exc)
        return None


def list_recent_messages(service, query: str, max_results: int):
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_detail(service, message_id: str) -> dict:
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()
