from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .backoff import next_delay_seconds
from .config import LOGGER, RuntimePaths
from .domain import AuthStatus, Checkpoint, MailEnvelope
from .errors import (
    MessageDecodeError,
    PermanentAuthError,
    SourceError,
    TransientSourceError,
)
from .gmail_client import (
    HttpError,
    get_gmail_service_with_status,
    get_message_detail,
    is_retryable_http_error,
    is_transient_network_error,
    list_recent_messages_page,
    notify_recovery_if_needed,
    record_transient_issue,
)
from .text import decode_mime_words

T = TypeVar("T")


class GmailClient(Protocol):
    @property
    def http_error_type(self) -> type[Exception]: ...

    def get_gmail_service_with_status(
        self,
        *,
        webhook_url: str | None,
        state: dict | None,
        state_file: Path | None,
        paths: RuntimePaths,
        transient_alert_min_duration_seconds: float,
        transient_alert_cooldown_seconds: float,
    ) -> tuple[Any | None, AuthStatus]: ...

    def list_recent_messages_page(
        self,
        service: Any,
        *,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, str]], str | None]: ...

    def get_message_detail(self, service: Any, message_id: str) -> dict[str, Any]: ...

    def notify_recovery_if_needed(
        self, webhook_url: str, state: dict, state_file: Path
    ) -> None: ...

    def record_transient_issue(
        self,
        state: dict,
        state_file: Path,
        err: Exception | str,
        *,
        webhook_url: str | None = None,
        alert_message: str | None = None,
        min_alert_duration_seconds: float,
        alert_cooldown_seconds: float,
    ) -> bool: ...

    def is_retryable_http_error(self, exc: Exception) -> bool: ...

    def is_transient_network_error(self, exc: Exception) -> bool: ...


@dataclass(frozen=True)
class GmailClientAdapter:
    get_gmail_service_with_status_fn: Callable[..., tuple[Any | None, AuthStatus]] = (
        get_gmail_service_with_status
    )
    list_recent_messages_page_fn: Callable[
        ..., tuple[list[dict[str, str]], str | None]
    ] = list_recent_messages_page
    get_message_detail_fn: Callable[[Any, str], dict[str, Any]] = get_message_detail
    notify_recovery_if_needed_fn: Callable[[str, dict, Path], None] = (
        notify_recovery_if_needed
    )
    record_transient_issue_fn: Callable[..., bool] = record_transient_issue
    is_retryable_http_error_fn: Callable[[Exception], bool] = is_retryable_http_error
    is_transient_network_error_fn: Callable[[Exception], bool] = (
        is_transient_network_error
    )
    http_error_type: type[Exception] = HttpError

    def get_gmail_service_with_status(
        self, **kwargs
    ) -> tuple[Any | None, AuthStatus]:
        return self.get_gmail_service_with_status_fn(**kwargs)

    def list_recent_messages_page(
        self,
        service: Any,
        *,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, str]], str | None]:
        return self.list_recent_messages_page_fn(
            service,
            query=query,
            max_results=max_results,
            page_token=page_token,
        )

    def get_message_detail(self, service: Any, message_id: str) -> dict[str, Any]:
        return self.get_message_detail_fn(service, message_id)

    def notify_recovery_if_needed(
        self, webhook_url: str, state: dict, state_file: Path
    ) -> None:
        self.notify_recovery_if_needed_fn(webhook_url, state, state_file)

    def record_transient_issue(
        self,
        state: dict,
        state_file: Path,
        err: Exception | str,
        *,
        webhook_url: str | None = None,
        alert_message: str | None = None,
        min_alert_duration_seconds: float,
        alert_cooldown_seconds: float,
    ) -> bool:
        return self.record_transient_issue_fn(
            state,
            state_file,
            err,
            webhook_url=webhook_url,
            alert_message=alert_message,
            min_alert_duration_seconds=min_alert_duration_seconds,
            alert_cooldown_seconds=alert_cooldown_seconds,
        )

    def is_retryable_http_error(self, exc: Exception) -> bool:
        return self.is_retryable_http_error_fn(exc)

    def is_transient_network_error(self, exc: Exception) -> bool:
        return self.is_transient_network_error_fn(exc)


@dataclass
class GmailMailSource:
    discord_webhook_url: str
    state: dict
    state_file: Path
    dry_run: bool
    gmail_api_max_retries: int
    gmail_api_base_delay_seconds: float
    gmail_api_max_delay_seconds: float
    runtime_paths: RuntimePaths
    transient_alert_min_duration_seconds: float
    transient_alert_cooldown_seconds: float
    gmail_client: GmailClient = field(default_factory=GmailClientAdapter)
    auth_status: AuthStatus = field(default=AuthStatus.READY, init=False)

    def get_auth_status(self) -> AuthStatus:
        return self.auth_status

    def notify_recovery_if_needed(self) -> None:
        if self.dry_run:
            return
        self.gmail_client.notify_recovery_if_needed(
            self.discord_webhook_url, self.state, self.state_file
        )

    def mark_transient_issue(self, err: Exception | str) -> None:
        if self.dry_run:
            return
        message = (
            "一時的な通信障害が継続しています。しばらく自動再試行を続けます。\n"
            f"エラー: {err}"
        )
        self.gmail_client.record_transient_issue(
            self.state,
            self.state_file,
            err,
            webhook_url=self.discord_webhook_url,
            alert_message=message,
            min_alert_duration_seconds=self.transient_alert_min_duration_seconds,
            alert_cooldown_seconds=self.transient_alert_cooldown_seconds,
        )

    def _call_gmail_api_with_retry(self, operation_name: str, fn: Callable[[], T]) -> T:
        if self.gmail_api_max_retries < 1:
            raise ValueError("gmail_api_max_retries must be >= 1")

        last_exc: Exception | None = None
        for attempt in range(1, self.gmail_api_max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                should_retry = self.gmail_client.is_transient_network_error(
                    exc
                ) or self.gmail_client.is_retryable_http_error(exc)
                if (not should_retry) or attempt == self.gmail_api_max_retries:
                    break
                delay = next_delay_seconds(
                    attempt,
                    base_delay=self.gmail_api_base_delay_seconds,
                    max_delay=self.gmail_api_max_delay_seconds,
                )
                LOGGER.warning(
                    "GMAIL_API_RETRY: op=%s attempt=%s/%s retry_in=%.2fs error=%s",
                    operation_name,
                    attempt,
                    self.gmail_api_max_retries,
                    delay,
                    exc,
                )
                time.sleep(delay)

        if last_exc is None:
            raise RuntimeError(f"{operation_name} failed without captured exception")
        raise last_exc

    def iter_new_messages(
        self, checkpoint: Checkpoint, max_messages: int
    ) -> Iterable[MailEnvelope]:
        # token refresh のタイミングを取りこぼさないため、run ごとに service を評価する。
        service, status = self.gmail_client.get_gmail_service_with_status(
            webhook_url=None if self.dry_run else self.discord_webhook_url,
            state=None if self.dry_run else self.state,
            state_file=None if self.dry_run else self.state_file,
            paths=self.runtime_paths,
            transient_alert_min_duration_seconds=self.transient_alert_min_duration_seconds,
            transient_alert_cooldown_seconds=self.transient_alert_cooldown_seconds,
        )
        self.auth_status = status
        if service is None:
            if status in {
                AuthStatus.REFRESH_TRANSIENT_FAILURE,
                AuthStatus.SERVICE_BUILD_TRANSIENT_FAILURE,
            }:
                raise TransientSourceError(
                    f"Gmail service 一時障害: auth_status={status.value}"
                )
            raise PermanentAuthError(
                f"Gmail service が利用できません。auth_status={status.value}"
            )

        list_page_size = min(500, max(max_messages, 100))

        def _safe_fetch_page(
            page_token: str | None,
        ) -> tuple[list[dict[str, str]], str | None]:
            try:

                def _list_page(
                    _page_token: str | None = page_token,
                ) -> tuple[list[dict[str, str]], str | None]:
                    return self.gmail_client.list_recent_messages_page(
                        service,
                        query="in:inbox",
                        max_results=list_page_size,
                        page_token=_page_token,
                    )

                return self._call_gmail_api_with_retry(
                    "list_recent_messages", _list_page
                )
            except Exception as exc:
                if isinstance(exc, self.gmail_client.http_error_type):
                    if self.gmail_client.is_retryable_http_error(exc):
                        raise TransientSourceError(
                            f"Gmail API 一時エラー: {exc}"
                        ) from exc
                    raise SourceError(f"Gmail API 恒久エラー: {exc}") from exc
                if self.gmail_client.is_transient_network_error(exc):
                    raise TransientSourceError(str(exc)) from exc
                raise SourceError(f"Gmail API 予期しないエラー: {exc}") from exc

        pending_messages: list[dict[str, str]] = []
        page_token: str | None = None
        checkpoint_found = checkpoint.message_id is None
        scanned_messages = 0
        page_count = 0

        while True:
            messages, next_page_token = _safe_fetch_page(page_token)
            page_count += 1

            if not messages:
                if page_count == 1:
                    LOGGER.info("RUN_ONCE_NO_MESSAGES")
                break

            for msg_meta in messages:
                msg_id = msg_meta["id"]
                if checkpoint.message_id and msg_id == checkpoint.message_id:
                    checkpoint_found = True
                    break
                pending_messages.append(msg_meta)
                scanned_messages += 1

            if checkpoint_found:
                break
            if next_page_token is None:
                break
            page_token = next_page_token

        if checkpoint.message_id and not checkpoint_found:
            raise SourceError(
                "checkpoint_not_found_in_listing: "
                f"checkpoint={checkpoint.message_id} scanned={scanned_messages} pages={page_count}"
            )

        if not pending_messages:
            LOGGER.info("RUN_ONCE_NO_NEW_MESSAGES")
            return

        pending_messages.reverse()
        if len(pending_messages) > max_messages:
            LOGGER.warning(
                "RUN_ONCE_PENDING_TRUNCATED_FRONTIER: pending=%s max_messages=%s",
                len(pending_messages),
                max_messages,
            )
            pending_messages = pending_messages[:max_messages]

        for msg_meta in pending_messages:
            msg_id = msg_meta["id"]
            try:

                def _get_detail(_message_id: str = msg_id) -> dict[str, Any]:
                    return self.gmail_client.get_message_detail(service, _message_id)

                msg = self._call_gmail_api_with_retry(
                    "get_message_detail",
                    _get_detail,
                )
            except Exception as exc:
                raise MessageDecodeError(
                    f"メッセージ詳細の取得に失敗しました: {exc}",
                    msg_id,
                ) from exc

            headers = msg.get("payload", {}).get("headers", [])
            header_dict = {header["name"]: header["value"] for header in headers}
            yield MailEnvelope(
                message_id=msg_id,
                subject=decode_mime_words(header_dict.get("Subject", "(no subject)")),
                from_header=decode_mime_words(header_dict.get("From", "(unknown)")),
                snippet=msg.get("snippet", ""),
            )
