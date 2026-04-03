import socket
import time
from pathlib import Path
from typing import Any, NoReturn

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GOOGLE_IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:
    GOOGLE_IMPORT_ERROR = exc

    def _raise_google_import_error() -> NoReturn:
        if GOOGLE_IMPORT_ERROR is None:
            raise ModuleNotFoundError("Google API libraries are missing.")
        raise GOOGLE_IMPORT_ERROR

    class Credentials:  # type: ignore[no-redef]
        valid = False
        expired = False
        refresh_token: str | None = None

        @classmethod
        def from_authorized_user_file(cls, *_args, **_kwargs) -> NoReturn:
            _raise_google_import_error()

        def __getattr__(self, _name: str) -> NoReturn:
            _raise_google_import_error()

        def refresh(self, *_args, **_kwargs) -> NoReturn:
            _raise_google_import_error()

        def to_json(self) -> str:
            _raise_google_import_error()

    class InstalledAppFlow:  # type: ignore[no-redef]
        @staticmethod
        def from_client_secrets_file(*_args, **_kwargs) -> NoReturn:
            _raise_google_import_error()

    def build(*_args, **_kwargs) -> NoReturn:  # type: ignore[no-redef]
        _raise_google_import_error()

    def Request(*_args, **_kwargs) -> NoReturn:  # type: ignore[no-redef]
        _raise_google_import_error()

    class HttpError(Exception):  # type: ignore[no-redef]
        """Fallback error type when googleapiclient is unavailable."""

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


def mark_transient_network_issue(state: dict, state_file: Path, err: Exception | str) -> None:
    state["transient_network_issue_active"] = True
    state["last_transient_error"] = str(err)
    state["last_transient_error_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_file, state)


def notify_recovery_if_needed(webhook_url: str, state: dict, state_file: Path) -> None:
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


def mark_token_issue(state: dict, state_file: Path, reason: str) -> bool:
    previous_active = bool(state.get("token_issue_active"))
    previous_reason = state.get("token_issue_reason")

    state["token_issue_active"] = True
    state["token_issue_reason"] = reason
    state["token_issue_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_file, state)

    return (not previous_active) or (previous_reason != reason)


def notify_token_recovery_if_needed(webhook_url: str | None, state: dict, state_file: Path) -> None:
    if not state.get("token_issue_active"):
        return

    if not webhook_url:
        LOGGER.warning("TOKEN_RECOVERY_NOTIFICATION_SKIPPED: missing_webhook")
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


def is_transient_network_error(exc: Exception, max_depth: int = 10) -> bool:
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

    current: BaseException | None = exc
    visited: set[int] = set()
    depth = 0
    while current and id(current) not in visited and depth < max_depth:
        visited.add(id(current))
        text = f"{type(current).__name__}: {current}".lower()
        if any(keyword in text for keyword in transient_keywords):
            return True
        current = current.__cause__ or current.__context__
        depth += 1

    return False


def refresh_with_retry(creds: Credentials, retries: int = 3, base_delay: int = 2) -> Exception | None:
    if retries < 1:
        raise ValueError("retries must be >= 1")

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            creds.refresh(Request())
            return None
        except Exception as exc:
            last_exc = exc
            is_transient = is_transient_network_error(exc)
            if (not is_transient) or attempt == retries:
                break

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


def _record_token_issue_and_maybe_alert(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    reason: str,
    alert_message: str,
) -> None:
    # token 問題は state を使って重複通知を抑制する。
    if not webhook_url or state is None or state_file is None:
        return
    should_alert = mark_token_issue(state, state_file, reason)
    if should_alert:
        send_discord_alert(webhook_url, alert_message)


def _record_transient_issue(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    error: Exception | str,
    alert_message: str,
) -> None:
    # transient 問題は発生のたびに通知し、運用監視で検知しやすくする。
    if webhook_url:
        send_discord_alert(webhook_url, alert_message)
    if state is not None and state_file is not None:
        mark_transient_network_issue(state, state_file, error)


def _load_initial_credentials(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
) -> Credentials | None:
    if not app_config.TOKEN_PATH.exists():
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_MISSING_INTERACTIVE_AUTH_START")
            return run_oauth_flow()

        reason = f"token.json が見つかりません: {app_config.TOKEN_PATH}"
        LOGGER.error("TOKEN_MISSING: %s", reason)
        _record_token_issue_and_maybe_alert(
            webhook_url,
            state,
            state_file,
            reason,
            "token.json が存在しないため Gmail API に接続できません。"
            " `amazon-notify --reauth` で再認証してください。",
        )
        return None

    try:
        return Credentials.from_authorized_user_file(str(app_config.TOKEN_PATH), SCOPES)
    except Exception as exc:
        reason = f"token.json の読み込みに失敗: {exc}"
        LOGGER.error("TOKEN_INVALID: %s", reason)
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_INVALID_INTERACTIVE_AUTH_START")
            return run_oauth_flow()

        _record_token_issue_and_maybe_alert(
            webhook_url,
            state,
            state_file,
            reason,
            "token.json の読み込みに失敗しました。"
            " `amazon-notify --reauth` で再認証してください。",
        )
        return None


def _ensure_usable_credentials(
    creds: Credentials,
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
) -> Credentials | None:
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        LOGGER.info("TOKEN_REFRESH_START")
        refresh_error = refresh_with_retry(creds)
        if refresh_error is None:
            with app_config.TOKEN_PATH.open("w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            LOGGER.info("TOKEN_REFRESH_SUCCESS: %s", app_config.TOKEN_PATH)
            return creds

        if is_transient_network_error(refresh_error):
            error_msg = (
                "トークン更新時に一時的な通信障害が発生しました。"
                "今回の実行はスキップし、次周期で自動復旧を待ちます。\n"
                f"エラー: {refresh_error}"
            )
            LOGGER.warning("TOKEN_REFRESH_TRANSIENT_FAILURE: %s", refresh_error)
            _record_transient_issue(
                webhook_url,
                state,
                state_file,
                refresh_error,
                error_msg,
            )
            return None

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
            return run_oauth_flow()

        _record_token_issue_and_maybe_alert(
            webhook_url,
            state,
            state_file,
            reason,
            error_msg,
        )
        return None

    reason = "token が無効で refresh_token も利用できません"
    LOGGER.error("TOKEN_INVALID_NO_REFRESH: %s", reason)
    if allow_oauth_interactive:
        LOGGER.info("TOKEN_INVALID_NO_REFRESH_INTERACTIVE_AUTH_START")
        return run_oauth_flow()

    _record_token_issue_and_maybe_alert(
        webhook_url,
        state,
        state_file,
        reason,
        "token が無効で自動更新できません。"
        " `amazon-notify --reauth` で再認証してください。",
    )
    return None


def _build_gmail_service(
    creds: Credentials,
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
):
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
            _record_transient_issue(
                webhook_url,
                state,
                state_file,
                exc,
                error_msg,
            )
            return None
        LOGGER.error("GMAIL_SERVICE_BUILD_FAILED: %s", exc)
        return None


def get_gmail_service(
    webhook_url: str | None = None,
    state: dict | None = None,
    state_file: Path | None = None,
    allow_oauth_interactive: bool = False,
):
    try:
        ensure_google_dependencies()
    except ModuleNotFoundError as exc:
        LOGGER.error("DEPENDENCY_MISSING: %s", exc)
        return None

    creds = _load_initial_credentials(
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
    )
    if not creds:
        return None

    usable_creds = _ensure_usable_credentials(
        creds,
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
    )
    if not usable_creds:
        return None

    return _build_gmail_service(
        creds=usable_creds,
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
    )


def list_recent_messages(service: Any, query: str, max_results: int) -> list[dict[str, str]]:
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_detail(service: Any, message_id: str) -> dict[str, Any]:
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full",
    ).execute()
