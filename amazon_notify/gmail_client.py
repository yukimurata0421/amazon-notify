import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, NoReturn

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None  # type: ignore[assignment]

try:
    from requests import exceptions as requests_exceptions
except ModuleNotFoundError:
    requests_exceptions = None  # type: ignore[assignment]

try:
    from urllib3 import exceptions as urllib3_exceptions
except ModuleNotFoundError:
    urllib3_exceptions = None  # type: ignore[assignment]

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
from .backoff import next_delay_seconds
from .config import LOGGER, RuntimePaths, load_state, save_state
from .discord_client import send_discord_alert, send_discord_recovery
from .domain import AuthStatus
from .time_utils import utc_now_iso

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS = 600.0
DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS = 1800.0


def _collect_exception_types(module: Any, names: tuple[str, ...]) -> tuple[type[BaseException], ...]:
    collected: list[type[BaseException]] = []
    for name in names:
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            collected.append(candidate)
    return tuple(collected)


_LIBRARY_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = ()
if requests_exceptions is not None:
    _LIBRARY_TRANSIENT_EXCEPTION_TYPES += _collect_exception_types(
        requests_exceptions,
        (
            "ConnectionError",
            "Timeout",
            "ReadTimeout",
            "ConnectTimeout",
            "SSLError",
        ),
    )
if urllib3_exceptions is not None:
    _LIBRARY_TRANSIENT_EXCEPTION_TYPES += _collect_exception_types(
        urllib3_exceptions,
        (
            "ProtocolError",
            "ReadTimeoutError",
            "ConnectTimeoutError",
            "NewConnectionError",
            "MaxRetryError",
        ),
    )


def _resolve_runtime_paths(paths: RuntimePaths | None) -> RuntimePaths:
    return app_config.get_runtime_paths() if paths is None else paths


@contextmanager
def _state_update_lock(state_file: Path):
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


def _sync_state_from_source(target: dict, source: dict) -> None:
    target.clear()
    target.update(source)


def ensure_google_dependencies() -> None:
    if GOOGLE_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "Google API libraries are missing. Install runtime deps with: "
            "`pip install .`"
        ) from GOOGLE_IMPORT_ERROR


def run_oauth_flow(paths: RuntimePaths | None = None) -> Credentials | None:
    runtime_paths = _resolve_runtime_paths(paths)
    try:
        ensure_google_dependencies()
        flow = InstalledAppFlow.from_client_secrets_file(
            str(runtime_paths.credentials),
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

    with runtime_paths.token.open("w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())
        LOGGER.info("TOKEN_SAVED: %s", runtime_paths.token)

    return creds


def _clear_transient_issue_keys(state: dict) -> None:
    state["transient_network_issue_active"] = False
    state.pop("last_transient_error", None)
    state.pop("last_transient_error_at", None)
    state.pop("transient_network_issue_first_seen_at_epoch", None)
    state.pop("transient_network_issue_last_seen_at_epoch", None)
    state.pop("transient_network_issue_occurrences", None)
    state.pop("transient_network_issue_last_alert_at_epoch", None)
    state.pop("transient_network_issue_notified", None)


def _should_send_transient_alert(
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

    with _state_update_lock(state_file):
        persisted_state = load_state(state_file)
        now_epoch = time.time()
        persisted_state["transient_network_issue_active"] = True
        persisted_state["last_transient_error"] = str(err)
        persisted_state["last_transient_error_at"] = utc_now_iso()
        first_seen = persisted_state.get("transient_network_issue_first_seen_at_epoch")
        if not isinstance(first_seen, (int, float)):
            first_seen = now_epoch
        persisted_state["transient_network_issue_first_seen_at_epoch"] = float(first_seen)
        persisted_state["transient_network_issue_last_seen_at_epoch"] = float(now_epoch)
        persisted_state["transient_network_issue_occurrences"] = int(
            persisted_state.get("transient_network_issue_occurrences", 0)
        ) + 1

        sent_alert = False
        if webhook_url and alert_message and _should_send_transient_alert(
            persisted_state,
            now_epoch=now_epoch,
            min_alert_duration_seconds=min_alert_duration_seconds,
            alert_cooldown_seconds=alert_cooldown_seconds,
        ):
            sent_alert = send_discord_alert(webhook_url, alert_message)
            if sent_alert:
                persisted_state["transient_network_issue_notified"] = True
                persisted_state["transient_network_issue_last_alert_at_epoch"] = float(now_epoch)

        save_state(state_file, persisted_state)
        _sync_state_from_source(state, persisted_state)
        return sent_alert


def mark_transient_network_issue(state: dict, state_file: Path, err: Exception | str) -> None:
    record_transient_issue(state, state_file, err)


def notify_recovery_if_needed(webhook_url: str, state: dict, state_file: Path) -> None:
    with _state_update_lock(state_file):
        persisted_state = load_state(state_file)
        _sync_state_from_source(state, persisted_state)

        if not persisted_state.get("transient_network_issue_active"):
            return
        if not persisted_state.get("transient_network_issue_notified"):
            _clear_transient_issue_keys(persisted_state)
            save_state(state_file, persisted_state)
            _sync_state_from_source(state, persisted_state)
            return

        message = (
            "一時的な通信障害から復旧しました。Gmail監視を再開しています。\n"
            f"前回障害時刻: {persisted_state.get('last_transient_error_at', '(unknown)')}\n"
            f"前回エラー: {persisted_state.get('last_transient_error', '(unknown)')}"
        )
        if not send_discord_recovery(webhook_url, message):
            LOGGER.warning("TRANSIENT_RECOVERY_NOTIFICATION_SKIPPED")
            return

        _clear_transient_issue_keys(persisted_state)
        save_state(state_file, persisted_state)
        _sync_state_from_source(state, persisted_state)


def mark_token_issue(state: dict, state_file: Path, reason: str) -> bool:
    previous_active = bool(state.get("token_issue_active"))
    previous_reason = state.get("token_issue_reason")

    state["token_issue_active"] = True
    state["token_issue_reason"] = reason
    state["token_issue_at"] = utc_now_iso()
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
    if _LIBRARY_TRANSIENT_EXCEPTION_TYPES and isinstance(exc, _LIBRARY_TRANSIENT_EXCEPTION_TYPES):
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
        "too many requests",
        "rate limit",
        "quota exceeded",
        "temporarily unavailable",
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


def is_retryable_http_error(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return False

    status = getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(status, int) and status in _RETRYABLE_HTTP_STATUS_CODES:
        return True
    return is_transient_network_error(exc)


def refresh_with_retry(
    creds: Credentials,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    request_factory: Callable[[], Any] | None = None,
) -> Exception | None:
    if retries < 1:
        raise ValueError("retries must be >= 1")
    if request_factory is None:
        ensure_google_dependencies()
        request_factory = Request

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            creds.refresh(request_factory())
            return None
        except Exception as exc:
            last_exc = exc
            is_transient = is_transient_network_error(exc) or is_retryable_http_error(exc)
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


def _record_token_issue_and_maybe_alert(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    reason: str,
    alert_message: str,
) -> None:
    # token 問題は state を使って重複通知を抑制する。
    if state is None or state_file is None:
        return
    should_alert = mark_token_issue(state, state_file, reason)
    if webhook_url and should_alert:
        send_discord_alert(webhook_url, alert_message)


def _record_transient_issue(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    error: Exception | str,
    alert_message: str,
    min_alert_duration_seconds: float,
    alert_cooldown_seconds: float,
) -> None:
    # transient 問題は持続時のみ通知して alert fatigue を抑える。
    if state is None or state_file is None:
        if webhook_url:
            send_discord_alert(webhook_url, alert_message)
        return
    record_transient_issue(
        state,
        state_file,
        error,
        webhook_url=webhook_url,
        alert_message=alert_message,
        min_alert_duration_seconds=min_alert_duration_seconds,
        alert_cooldown_seconds=alert_cooldown_seconds,
    )


def _load_initial_credentials(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
    runtime_paths: RuntimePaths,
) -> tuple[Credentials | None, AuthStatus]:
    if not runtime_paths.token.exists():
        if allow_oauth_interactive:
            LOGGER.info("TOKEN_MISSING_INTERACTIVE_AUTH_START")
            return run_oauth_flow(runtime_paths), AuthStatus.TOKEN_MISSING

        reason = f"token.json が見つかりません: {runtime_paths.token}"
        LOGGER.error("TOKEN_MISSING: %s", reason)
        _record_token_issue_and_maybe_alert(
            webhook_url,
            state,
            state_file,
            reason,
            "token.json が存在しないため Gmail API に接続できません。"
            " `amazon-notify --reauth` で再認証してください。",
        )
        return None, AuthStatus.TOKEN_MISSING

    try:
        creds = Credentials.from_authorized_user_file(str(runtime_paths.token), SCOPES)
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
            return run_oauth_flow(runtime_paths), AuthStatus.TOKEN_CORRUPTED

        _record_token_issue_and_maybe_alert(
            webhook_url,
            state,
            state_file,
            reason,
            "token.json の読み込みに失敗しました。"
            " `amazon-notify --reauth` で再認証してください。",
        )
        return None, AuthStatus.TOKEN_CORRUPTED


def _ensure_usable_credentials(
    creds: Credentials,
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
    runtime_paths: RuntimePaths,
    transient_alert_min_duration_seconds: float,
    transient_alert_cooldown_seconds: float,
) -> tuple[Credentials | None, AuthStatus]:
    if creds.valid:
        return creds, AuthStatus.TOKEN_VALID

    if creds.expired and creds.refresh_token:
        LOGGER.info("TOKEN_REFRESH_START")
        refresh_error = refresh_with_retry(creds)
        if refresh_error is None:
            with runtime_paths.token.open("w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            LOGGER.info("TOKEN_REFRESH_SUCCESS: %s", runtime_paths.token)
            return creds, AuthStatus.TOKEN_VALID

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
                send_discord_alert(webhook_url, error_msg)
            return run_oauth_flow(runtime_paths), AuthStatus.REFRESH_PERMANENT_FAILURE

        _record_token_issue_and_maybe_alert(
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
        return run_oauth_flow(runtime_paths), AuthStatus.INTERACTIVE_REAUTH_REQUIRED

    _record_token_issue_and_maybe_alert(
        webhook_url,
        state,
        state_file,
        reason,
        "token が無効で自動更新できません。"
        " `amazon-notify --reauth` で再認証してください。",
    )
    return None, AuthStatus.INTERACTIVE_REAUTH_REQUIRED


def _build_gmail_service(
    creds: Credentials,
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    transient_alert_min_duration_seconds: float,
    transient_alert_cooldown_seconds: float,
) -> tuple[Any | None, AuthStatus]:
    try:
        # file_cache 警告を避けるため discovery cache は明示的に無効化する。
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        if state is not None and state_file is not None:
            notify_token_recovery_if_needed(webhook_url, state, state_file)
        return service, AuthStatus.READY
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
                transient_alert_min_duration_seconds,
                transient_alert_cooldown_seconds,
            )
            return None, AuthStatus.SERVICE_BUILD_TRANSIENT_FAILURE
        LOGGER.error("GMAIL_SERVICE_BUILD_FAILED: %s", exc)
        return None, AuthStatus.INTERACTIVE_REAUTH_REQUIRED


def start_gmail_watch(
    service: Any,
    *,
    topic_name: str,
    label_ids: list[str] | None = None,
    label_filter_action: str = "include",
) -> dict[str, Any]:
    action = label_filter_action.strip().lower()
    if action not in {"include", "exclude"}:
        raise ValueError("label_filter_action must be 'include' or 'exclude'")

    body: dict[str, Any] = {"topicName": topic_name}
    if label_ids:
        body["labelIds"] = label_ids
        body["labelFilterAction"] = action.upper()

    return service.users().watch(userId="me", body=body).execute()


def start_gmail_watch_with_retry(
    service: Any,
    *,
    topic_name: str,
    label_ids: list[str] | None = None,
    label_filter_action: str = "include",
    retries: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> dict[str, Any]:
    if retries < 1:
        raise ValueError("retries must be >= 1")

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return start_gmail_watch(
                service,
                topic_name=topic_name,
                label_ids=label_ids,
                label_filter_action=label_filter_action,
            )
        except Exception as exc:
            last_exc = exc
            should_retry = is_transient_network_error(exc) or is_retryable_http_error(exc)
            if (not should_retry) or attempt == retries:
                break
            delay = next_delay_seconds(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            LOGGER.warning(
                "GMAIL_WATCH_RETRY: attempt=%s/%s retry_in=%.2fs error=%s",
                attempt,
                retries,
                delay,
                exc,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def get_gmail_service_with_status(
    webhook_url: str | None = None,
    state: dict | None = None,
    state_file: Path | None = None,
    allow_oauth_interactive: bool = False,
    paths: RuntimePaths | None = None,
    transient_alert_min_duration_seconds: float = DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS,
    transient_alert_cooldown_seconds: float = DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS,
) -> tuple[Any | None, AuthStatus]:
    try:
        ensure_google_dependencies()
    except ModuleNotFoundError as exc:
        LOGGER.error("DEPENDENCY_MISSING: %s", exc)
        return None, AuthStatus.INTERACTIVE_REAUTH_REQUIRED

    runtime_paths = _resolve_runtime_paths(paths)
    creds, initial_status = _load_initial_credentials(
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
        runtime_paths=runtime_paths,
    )
    if not creds:
        return None, initial_status

    usable_creds, usable_status = _ensure_usable_credentials(
        creds,
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
        runtime_paths=runtime_paths,
        transient_alert_min_duration_seconds=transient_alert_min_duration_seconds,
        transient_alert_cooldown_seconds=transient_alert_cooldown_seconds,
    )
    if not usable_creds:
        return None, usable_status

    service, service_status = _build_gmail_service(
        creds=usable_creds,
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        transient_alert_min_duration_seconds=transient_alert_min_duration_seconds,
        transient_alert_cooldown_seconds=transient_alert_cooldown_seconds,
    )
    return service, service_status


def get_gmail_service(
    webhook_url: str | None = None,
    state: dict | None = None,
    state_file: Path | None = None,
    allow_oauth_interactive: bool = False,
    paths: RuntimePaths | None = None,
    transient_alert_min_duration_seconds: float = DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS,
    transient_alert_cooldown_seconds: float = DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS,
):
    service, _status = get_gmail_service_with_status(
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
        paths=paths,
        transient_alert_min_duration_seconds=transient_alert_min_duration_seconds,
        transient_alert_cooldown_seconds=transient_alert_cooldown_seconds,
    )
    return service


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
