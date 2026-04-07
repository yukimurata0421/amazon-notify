import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

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

    def build(*_args, **_kwargs) -> NoReturn:
        _raise_google_import_error()

    def Request(*_args, **_kwargs) -> NoReturn:  # type: ignore[no-redef]
        _raise_google_import_error()

    class HttpError(Exception):  # type: ignore[no-redef]
        """Fallback error type when googleapiclient is unavailable."""


from . import config as app_config
from . import gmail_auth, gmail_transient_state
from .backoff import next_delay_seconds
from .config import LOGGER, RuntimePaths
from .discord_client import send_discord_alert, send_discord_recovery
from .domain import AuthStatus
from .gmail_api import (
    get_message_detail as get_message_detail_impl,
)
from .gmail_api import (
    list_recent_messages as list_recent_messages_impl,
)
from .gmail_api import (
    list_recent_messages_page as list_recent_messages_page_impl,
)
from .gmail_api import (
    start_gmail_watch as start_gmail_watch_impl,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_DISCORD_DEDUPE_STATE_FILENAME = ".discord_dedupe_state.json"
DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS = (
    gmail_transient_state.DEFAULT_TRANSIENT_ALERT_MIN_DURATION_SECONDS
)
DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS = (
    gmail_transient_state.DEFAULT_TRANSIENT_ALERT_COOLDOWN_SECONDS
)


def _collect_exception_types(
    module: Any, names: tuple[str, ...]
) -> tuple[type[BaseException], ...]:
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


def _dedupe_state_path_for_state_file(state_file: Path) -> Path:
    return state_file.parent / _DISCORD_DEDUPE_STATE_FILENAME


def _send_discord_alert_with_dedupe(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None,
) -> bool:
    try:
        return send_discord_alert(
            webhook_url,
            message,
            dedupe_state_path=dedupe_state_path,
        )
    except TypeError:
        # Test doubles may keep the legacy 2-arg shape.
        return send_discord_alert(webhook_url, message)


def _send_discord_recovery_with_dedupe(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None,
) -> bool:
    try:
        return send_discord_recovery(
            webhook_url,
            message,
            dedupe_state_path=dedupe_state_path,
        )
    except TypeError:
        # Test doubles may keep the legacy 2-arg shape.
        return send_discord_recovery(webhook_url, message)


def ensure_google_dependencies() -> None:
    if GOOGLE_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "Google API libraries are missing. Install runtime deps with: "
            "`pip install .`"
        ) from GOOGLE_IMPORT_ERROR


def run_oauth_flow(paths: RuntimePaths | None = None) -> Credentials | None:
    runtime_paths = _resolve_runtime_paths(paths)
    creds = gmail_auth.run_oauth_flow(
        runtime_paths=runtime_paths,
        scopes=SCOPES,
        ensure_google_dependencies_fn=ensure_google_dependencies,
        flow_factory=InstalledAppFlow,
    )
    if creds is None:
        return None
    return creds


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
    dedupe_state_path = _dedupe_state_path_for_state_file(state_file)
    return gmail_transient_state.record_transient_issue(
        state,
        state_file,
        err,
        webhook_url=webhook_url,
        alert_message=alert_message,
        min_alert_duration_seconds=min_alert_duration_seconds,
        alert_cooldown_seconds=alert_cooldown_seconds,
        send_discord_alert_fn=lambda webhook_url, message: (
            _send_discord_alert_with_dedupe(
                webhook_url,
                message,
                dedupe_state_path=dedupe_state_path,
            )
        ),
    )


def mark_transient_network_issue(
    state: dict, state_file: Path, err: Exception | str
) -> None:
    record_transient_issue(state, state_file, err)


def notify_recovery_if_needed(webhook_url: str, state: dict, state_file: Path) -> None:
    dedupe_state_path = _dedupe_state_path_for_state_file(state_file)
    gmail_transient_state.notify_recovery_if_needed(
        webhook_url,
        state,
        state_file,
        send_discord_recovery_fn=lambda _webhook_url, message: (
            _send_discord_recovery_with_dedupe(
                _webhook_url,
                message,
                dedupe_state_path=dedupe_state_path,
            )
        ),
    )


def mark_token_issue(state: dict, state_file: Path, reason: str) -> bool:
    return gmail_transient_state.mark_token_issue(state, state_file, reason)


def notify_token_recovery_if_needed(
    webhook_url: str | None, state: dict, state_file: Path
) -> None:
    dedupe_state_path = _dedupe_state_path_for_state_file(state_file)
    gmail_transient_state.notify_token_recovery_if_needed(
        webhook_url,
        state,
        state_file,
        send_discord_recovery_fn=lambda _webhook_url, message: (
            _send_discord_recovery_with_dedupe(
                _webhook_url,
                message,
                dedupe_state_path=dedupe_state_path,
            )
        ),
    )


def is_transient_network_error(exc: Exception, max_depth: int = 10) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, socket.gaierror)):
        return True
    if _LIBRARY_TRANSIENT_EXCEPTION_TYPES and isinstance(
        exc, _LIBRARY_TRANSIENT_EXCEPTION_TYPES
    ):
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
    return gmail_auth.refresh_with_retry(
        creds,
        retries=retries,
        base_delay=base_delay,
        max_delay=max_delay,
        request_factory=request_factory,
        ensure_google_dependencies_fn=ensure_google_dependencies,
        default_request_factory=Request,
        is_transient_network_error_fn=is_transient_network_error,
        is_retryable_http_error_fn=is_retryable_http_error,
    )


def _record_token_issue_and_maybe_alert(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    reason: str,
    alert_message: str,
) -> None:
    dedupe_state_path = (
        _dedupe_state_path_for_state_file(state_file)
        if state_file is not None
        else None
    )
    gmail_transient_state.record_token_issue_and_maybe_alert(
        webhook_url,
        state,
        state_file,
        reason,
        alert_message,
        send_discord_alert_fn=lambda _webhook_url, message: (
            _send_discord_alert_with_dedupe(
                _webhook_url,
                message,
                dedupe_state_path=dedupe_state_path,
            )
        ),
    )


def _record_transient_issue(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    error: Exception | str,
    alert_message: str,
    min_alert_duration_seconds: float,
    alert_cooldown_seconds: float,
) -> None:
    dedupe_state_path = (
        _dedupe_state_path_for_state_file(state_file)
        if state_file is not None
        else None
    )
    gmail_transient_state.record_transient_issue_or_alert(
        webhook_url,
        state,
        state_file,
        error,
        alert_message,
        min_alert_duration_seconds,
        alert_cooldown_seconds,
        send_discord_alert_fn=lambda _webhook_url, message: (
            _send_discord_alert_with_dedupe(
                _webhook_url,
                message,
                dedupe_state_path=dedupe_state_path,
            )
        ),
    )


def _load_initial_credentials(
    webhook_url: str | None,
    state: dict | None,
    state_file: Path | None,
    allow_oauth_interactive: bool,
    runtime_paths: RuntimePaths,
) -> tuple[Credentials | None, AuthStatus]:
    creds, status = gmail_auth.load_initial_credentials(
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
        runtime_paths=runtime_paths,
        scopes=SCOPES,
        credentials_cls=Credentials,
        run_oauth_flow_fn=run_oauth_flow,
        record_token_issue_and_maybe_alert_fn=_record_token_issue_and_maybe_alert,
    )
    return creds, status


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
    usable_creds, status = gmail_auth.ensure_usable_credentials(
        creds=creds,
        webhook_url=webhook_url,
        state=state,
        state_file=state_file,
        allow_oauth_interactive=allow_oauth_interactive,
        runtime_paths=runtime_paths,
        transient_alert_min_duration_seconds=transient_alert_min_duration_seconds,
        transient_alert_cooldown_seconds=transient_alert_cooldown_seconds,
        refresh_with_retry_fn=refresh_with_retry,
        is_transient_network_error_fn=is_transient_network_error,
        run_oauth_flow_fn=run_oauth_flow,
        record_transient_issue_fn=_record_transient_issue,
        record_token_issue_and_maybe_alert_fn=_record_token_issue_and_maybe_alert,
        send_discord_alert_fn=lambda _webhook_url, message: (
            _send_discord_alert_with_dedupe(
                _webhook_url,
                message,
                dedupe_state_path=runtime_paths.runtime_dir
                / _DISCORD_DEDUPE_STATE_FILENAME,
            )
        ),
    )
    return usable_creds, status


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
    return start_gmail_watch_impl(
        service,
        topic_name=topic_name,
        label_ids=label_ids,
        label_filter_action=label_filter_action,
    )


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
            should_retry = is_transient_network_error(exc) or is_retryable_http_error(
                exc
            )
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


def list_recent_messages(
    service: Any, query: str, max_results: int
) -> list[dict[str, str]]:
    return list_recent_messages_impl(
        service,
        query=query,
        max_results=max_results,
    )


def list_recent_messages_page(
    service: Any,
    *,
    query: str,
    max_results: int,
    page_token: str | None = None,
) -> tuple[list[dict[str, str]], str | None]:
    return list_recent_messages_page_impl(
        service,
        query=query,
        max_results=max_results,
        page_token=page_token,
    )


def get_message_detail(service: Any, message_id: str) -> dict[str, Any]:
    return get_message_detail_impl(service, message_id)
