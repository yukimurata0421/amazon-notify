from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from re import Pattern
from typing import Any, TypeVar

from .backoff import next_delay_seconds
from .checkpoint_store import JsonlCheckpointStore
from .config import LOGGER, RuntimePaths, get_runtime_paths, load_state
from .discord_client import (
    send_discord_alert,
    send_discord_notification,
    send_discord_recovery,
)
from .domain import (
    AuthStatus,
    Checkpoint,
    MailEnvelope,
    NotificationCandidate,
    RunResult,
)
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
    list_recent_messages,
    notify_recovery_if_needed,
    record_transient_issue,
)
from .pipeline import NotificationPipeline
from .runtime import RuntimeConfig
from .text import (
    build_gmail_message_url,
    decode_mime_words,
    extract_email_address,
    is_amazon_mail,
)

T = TypeVar("T")
_INCIDENT_MEMORY_SUPPRESSION_SECONDS = 1800.0
_INCIDENT_MEMORY_SUPPRESSED_UNTIL: dict[str, float] = {}


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
    auth_status: AuthStatus = field(default=AuthStatus.READY, init=False)

    def get_auth_status(self) -> AuthStatus:
        return self.auth_status

    def notify_recovery_if_needed(self) -> None:
        if self.dry_run:
            return
        notify_recovery_if_needed(self.discord_webhook_url, self.state, self.state_file)

    def mark_transient_issue(self, err: Exception | str) -> None:
        if self.dry_run:
            return
        message = (
            "一時的な通信障害が継続しています。しばらく自動再試行を続けます。\n"
            f"エラー: {err}"
        )
        record_transient_issue(
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
                should_retry = is_transient_network_error(exc) or is_retryable_http_error(exc)
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

        assert last_exc is not None
        raise last_exc

    def iter_new_messages(self, checkpoint: Checkpoint, max_messages: int) -> Iterable[MailEnvelope]:
        # token refresh のタイミングを取りこぼさないため、run ごとに service を評価する。
        service, status = get_gmail_service_with_status(
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

        try:
            def _list_recent() -> list[dict[str, str]]:
                return list_recent_messages(
                    service,
                    query="in:inbox",
                    max_results=max_messages,
                )

            messages = self._call_gmail_api_with_retry(
                "list_recent_messages",
                _list_recent,
            )
        except Exception as exc:
            if isinstance(exc, HttpError):
                if is_retryable_http_error(exc):
                    raise TransientSourceError(f"Gmail API 一時エラー: {exc}") from exc
                raise SourceError(f"Gmail API 恒久エラー: {exc}") from exc
            if is_transient_network_error(exc):
                raise TransientSourceError(str(exc)) from exc
            raise SourceError(f"Gmail API 予期しないエラー: {exc}") from exc

        if not messages:
            LOGGER.info("RUN_ONCE_NO_MESSAGES")
            return

        pending_messages: list[dict[str, str]] = []
        for msg_meta in messages:
            msg_id = msg_meta["id"]
            if checkpoint.message_id and msg_id == checkpoint.message_id:
                break
            pending_messages.append(msg_meta)

        if not pending_messages:
            LOGGER.info("RUN_ONCE_NO_NEW_MESSAGES")
            return

        pending_messages.reverse()
        for msg_meta in pending_messages:
            msg_id = msg_meta["id"]
            try:
                def _get_detail(_message_id: str = msg_id) -> dict[str, Any]:
                    return get_message_detail(service, _message_id)

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


@dataclass
class RegexClassifier:
    amazon_pattern: Pattern[str]
    subject_pattern: Pattern[str] | None

    def classify(self, envelope: MailEnvelope) -> NotificationCandidate | None:
        should_notify = is_amazon_mail(envelope.from_header, self.amazon_pattern)
        if should_notify and self.subject_pattern is not None:
            should_notify = self.subject_pattern.search(envelope.subject) is not None
        if not should_notify:
            return None
        return NotificationCandidate(
            envelope=envelope,
            from_addr=extract_email_address(envelope.from_header),
            url=build_gmail_message_url(envelope.message_id),
        )


@dataclass
class DiscordNotifier:
    webhook_url: str
    dry_run: bool
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float

    def notify(self, candidate: NotificationCandidate) -> bool:
        if self.dry_run:
            LOGGER.info(
                "DRY_RUN_NOTIFICATION: id=%s subject=%s from=%s",
                candidate.envelope.message_id,
                candidate.envelope.subject,
                candidate.from_addr,
            )
            return True

        return send_discord_notification(
            webhook_url=self.webhook_url,
            subject=candidate.envelope.subject,
            from_addr=candidate.from_addr,
            snippet=candidate.envelope.snippet,
            url=candidate.url,
            max_attempts=self.max_attempts,
            base_delay_seconds=self.base_delay_seconds,
            max_delay_seconds=self.max_delay_seconds,
        )


def _handle_incident_lifecycle(
    *,
    checkpoint_store: JsonlCheckpointStore,
    discord_webhook_url: str,
    dry_run: bool,
    result: RunResult,
) -> None:
    active_incident = checkpoint_store.load_incident_state()
    active_kind = active_incident["kind"] if active_incident else None
    failure_kind = result.failure_kind.value if result.failure_kind else None

    if result.failure_kind is not None and result.should_alert and not dry_run and discord_webhook_url:
        assert failure_kind is not None
        now_epoch = time.time()
        suppressed_until = _INCIDENT_MEMORY_SUPPRESSED_UNTIL.get(failure_kind)
        if suppressed_until is not None and now_epoch < suppressed_until:
            LOGGER.warning(
                "INCIDENT_ALERT_SUPPRESSED_IN_MEMORY: kind=%s remaining=%.1fs",
                failure_kind,
                suppressed_until - now_epoch,
            )
            return
        # 同一インシデント継続時は抑制して連投を避ける。
        if active_kind == failure_kind:
            try:
                checkpoint_store.suppress_incident(
                    kind=failure_kind,
                    run_id=result.run_id,
                )
                _INCIDENT_MEMORY_SUPPRESSED_UNTIL.pop(failure_kind, None)
            except OSError as exc:
                LOGGER.error(
                    "INCIDENT_SUPPRESS_STATE_WRITE_FAILED: run_id=%s kind=%s error=%s",
                    result.run_id,
                    failure_kind,
                    exc,
                )
                _INCIDENT_MEMORY_SUPPRESSED_UNTIL[failure_kind] = (
                    now_epoch + _INCIDENT_MEMORY_SUPPRESSION_SECONDS
                )
            return

        message = result.failure_message or failure_kind or "unknown failure"
        if result.failure_message_id:
            message = f"{message}\nmessage_id: {result.failure_message_id}"
        sent = send_discord_alert(discord_webhook_url, message)
        if sent:
            try:
                checkpoint_store.open_incident(
                    kind=failure_kind,
                    message=result.failure_message,
                    opened_at=result.ended_at,
                    run_id=result.run_id,
                )
                _INCIDENT_MEMORY_SUPPRESSED_UNTIL.pop(failure_kind, None)
            except OSError as exc:
                LOGGER.error(
                    "INCIDENT_OPEN_STATE_WRITE_FAILED: run_id=%s kind=%s error=%s",
                    result.run_id,
                    failure_kind,
                    exc,
                )
                _INCIDENT_MEMORY_SUPPRESSED_UNTIL[failure_kind] = (
                    now_epoch + _INCIDENT_MEMORY_SUPPRESSION_SECONDS
                )
        return

    # 正常化したら close 通知して incident を解消する。
    if result.failure_kind is None and active_kind and not dry_run and discord_webhook_url:
        assert active_incident is not None
        recovery_msg = (
            "障害状態から復旧しました。\n"
            f"kind: {active_kind}\n"
            f"suppressed_count: {active_incident['suppressed_count']}"
        )
        sent = send_discord_recovery(discord_webhook_url, recovery_msg)
        if sent:
            try:
                checkpoint_store.recover_incident(run_id=result.run_id)
            except OSError as exc:
                LOGGER.error(
                    "INCIDENT_RECOVER_STATE_WRITE_FAILED: run_id=%s kind=%s error=%s",
                    result.run_id,
                    active_kind,
                    exc,
                )


def run_once(runtime: RuntimeConfig) -> RunResult:
    discord_webhook_url = runtime.discord_webhook_url
    amazon_pattern = runtime.amazon_pattern
    state_file = runtime.state_file
    max_messages = runtime.max_messages
    subject_pattern = runtime.subject_pattern
    dry_run = runtime.dry_run
    events_file = runtime.events_file
    runs_file = runtime.runs_file
    runtime_paths_raw = runtime.runtime_paths

    state = load_state(state_file)
    LOGGER.info("RUN_ONCE_START: last_message_id=%s dry_run=%s", state.get("last_message_id"), dry_run)

    runtime_paths: RuntimePaths
    if isinstance(runtime_paths_raw, RuntimePaths):
        runtime_paths = runtime_paths_raw
    else:
        runtime_paths = get_runtime_paths()

    source = GmailMailSource(
        discord_webhook_url=discord_webhook_url,
        state=state,
        state_file=state_file,
        dry_run=dry_run,
        gmail_api_max_retries=runtime.gmail_api_max_retries,
        gmail_api_base_delay_seconds=runtime.gmail_api_base_delay_seconds,
        gmail_api_max_delay_seconds=runtime.gmail_api_max_delay_seconds,
        runtime_paths=runtime_paths,
        transient_alert_min_duration_seconds=runtime.transient_alert_min_duration_seconds,
        transient_alert_cooldown_seconds=runtime.transient_alert_cooldown_seconds,
    )
    classifier = RegexClassifier(
        amazon_pattern=amazon_pattern,
        subject_pattern=subject_pattern,
    )
    notifier = DiscordNotifier(
        webhook_url=discord_webhook_url,
        dry_run=dry_run,
        max_attempts=runtime.discord_max_retries,
        base_delay_seconds=runtime.discord_base_delay_seconds,
        max_delay_seconds=runtime.discord_max_delay_seconds,
    )
    checkpoint_store = JsonlCheckpointStore(
        state_file=state_file,
        events_file=events_file,
        runs_file=runs_file,
    )

    pipeline = NotificationPipeline(
        source=source,
        classifier=classifier,
        notifier=notifier,
        checkpoint_store=checkpoint_store,
        max_messages=max_messages,
        dry_run=dry_run,
    )
    result = pipeline.run_once()
    _handle_incident_lifecycle(
        checkpoint_store=checkpoint_store,
        discord_webhook_url=discord_webhook_url,
        dry_run=dry_run,
        result=result,
    )

    if result.notified_count == 0:
        LOGGER.info(
            "RUN_ONCE_COMPLETE: amazon_notifications=0 non_amazon_skipped=%s",
            result.non_target_count,
        )
    else:
        LOGGER.info(
            "RUN_ONCE_COMPLETE: amazon_notifications=%s non_amazon_skipped=%s",
            result.notified_count,
            result.non_target_count,
        )
    return result
