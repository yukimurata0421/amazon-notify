from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from re import Pattern

from .checkpoint_store import JsonlCheckpointStore
from .config import LOGGER, RuntimePaths, get_runtime_paths, load_state
from .discord_client import (
    send_discord_alert,
    send_discord_notification,
    send_discord_recovery,
)
from .domain import (
    AuthStatus,
    FailureKind,
    MailEnvelope,
    NotificationCandidate,
    RunResult,
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
from .gmail_source import GmailMailSource
from .pipeline import NotificationPipeline
from .runtime import RuntimeConfig
from .text import (
    build_gmail_message_url,
    extract_email_address,
    is_amazon_mail,
)
from .time_utils import utc_now_iso

_INCIDENT_MEMORY_SUPPRESSION_SECONDS = 1800.0
# Backward-compatible symbol export for existing tests/integrations.
_AUTH_STATUS_SYMBOL = AuthStatus


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
    incident_memory_suppressed_until: dict[str, float],
) -> None:
    active_incident = checkpoint_store.load_incident_state()
    active_kind = active_incident["kind"] if active_incident else None
    failure_kind = result.failure_kind.value if result.failure_kind else None

    if result.failure_kind is not None and result.should_alert and not dry_run and discord_webhook_url:
        assert failure_kind is not None
        now_epoch = time.time()
        suppressed_until = incident_memory_suppressed_until.get(failure_kind)
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
                incident_memory_suppressed_until.pop(failure_kind, None)
            except OSError as exc:
                LOGGER.error(
                    "INCIDENT_SUPPRESS_STATE_WRITE_FAILED: run_id=%s kind=%s error=%s",
                    result.run_id,
                    failure_kind,
                    exc,
                )
                incident_memory_suppressed_until[failure_kind] = (
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
                incident_memory_suppressed_until.pop(failure_kind, None)
            except OSError as exc:
                LOGGER.error(
                    "INCIDENT_OPEN_STATE_WRITE_FAILED: run_id=%s kind=%s error=%s",
                    result.run_id,
                    failure_kind,
                    exc,
                )
                incident_memory_suppressed_until[failure_kind] = (
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


def _incident_memory_map(runtime: RuntimeConfig) -> dict[str, float]:
    candidate = getattr(runtime, "incident_memory_suppressed_until", None)
    if isinstance(candidate, dict):
        return candidate
    return {}


def _resolve_runtime_paths(runtime: RuntimeConfig) -> RuntimePaths:
    runtime_paths = runtime.runtime_paths
    if isinstance(runtime_paths, RuntimePaths):
        return runtime_paths
    return get_runtime_paths()


def _build_pipeline(
    *,
    runtime: RuntimeConfig,
    state: dict,
    runtime_paths: RuntimePaths,
) -> tuple[NotificationPipeline, JsonlCheckpointStore]:
    source = GmailMailSource(
        discord_webhook_url=runtime.discord_webhook_url,
        state=state,
        state_file=runtime.state_file,
        dry_run=runtime.dry_run,
        gmail_api_max_retries=runtime.gmail_api_max_retries,
        gmail_api_base_delay_seconds=runtime.gmail_api_base_delay_seconds,
        gmail_api_max_delay_seconds=runtime.gmail_api_max_delay_seconds,
        runtime_paths=runtime_paths,
        transient_alert_min_duration_seconds=runtime.transient_alert_min_duration_seconds,
        transient_alert_cooldown_seconds=runtime.transient_alert_cooldown_seconds,
        get_gmail_service_with_status_fn=get_gmail_service_with_status,
        list_recent_messages_page_fn=list_recent_messages_page,
        get_message_detail_fn=get_message_detail,
        notify_recovery_if_needed_fn=notify_recovery_if_needed,
        record_transient_issue_fn=record_transient_issue,
        is_retryable_http_error_fn=is_retryable_http_error,
        is_transient_network_error_fn=is_transient_network_error,
        http_error_type=HttpError,
    )
    classifier = RegexClassifier(
        amazon_pattern=runtime.amazon_pattern,
        subject_pattern=runtime.subject_pattern,
    )
    notifier = DiscordNotifier(
        webhook_url=runtime.discord_webhook_url,
        dry_run=runtime.dry_run,
        max_attempts=runtime.discord_max_retries,
        base_delay_seconds=runtime.discord_base_delay_seconds,
        max_delay_seconds=runtime.discord_max_delay_seconds,
    )
    checkpoint_store = JsonlCheckpointStore(
        state_file=runtime.state_file,
        events_file=runtime.events_file,
        runs_file=runtime.runs_file,
    )
    pipeline = NotificationPipeline(
        source=source,
        classifier=classifier,
        notifier=notifier,
        checkpoint_store=checkpoint_store,
        max_messages=runtime.max_messages,
        dry_run=runtime.dry_run,
    )
    return pipeline, checkpoint_store


def run_once(runtime: RuntimeConfig) -> RunResult:
    discord_webhook_url = runtime.discord_webhook_url
    dry_run = runtime.dry_run
    incident_memory_suppressed_until = _incident_memory_map(runtime)

    state = load_state(runtime.state_file)
    LOGGER.info("RUN_ONCE_START: last_message_id=%s dry_run=%s", state.get("last_message_id"), dry_run)

    pipeline, checkpoint_store = _build_pipeline(
        runtime=runtime,
        state=state,
        runtime_paths=_resolve_runtime_paths(runtime),
    )
    result = pipeline.run_once()
    _handle_incident_lifecycle(
        checkpoint_store=checkpoint_store,
        discord_webhook_url=discord_webhook_url,
        dry_run=dry_run,
        result=result,
        incident_memory_suppressed_until=incident_memory_suppressed_until,
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


def report_unhandled_exception(runtime: RuntimeConfig, exc: Exception) -> RunResult:
    checkpoint_store = JsonlCheckpointStore(
        state_file=runtime.state_file,
        events_file=runtime.events_file,
        runs_file=runtime.runs_file,
    )
    checkpoint_before: str | None = None
    try:
        checkpoint_before = checkpoint_store.load_checkpoint().message_id
    except Exception as checkpoint_exc:
        LOGGER.error(
            "UNHANDLED_EXCEPTION_CHECKPOINT_LOAD_FAILED: error=%s",
            checkpoint_exc,
        )

    now = utc_now_iso()
    result = RunResult(
        run_id=uuid.uuid4().hex,
        started_at=now,
        ended_at=now,
        checkpoint_before=checkpoint_before,
        checkpoint_after=checkpoint_before,
        processed_count=0,
        matched_count=0,
        notified_count=0,
        non_target_count=0,
        failure_kind=FailureKind.SOURCE_FAILED,
        failure_message=str(exc),
        failure_message_id=None,
        should_retry=True,
        should_alert=True,
        auth_status=None,
    )

    try:
        checkpoint_store.append_event(
            "source_failed",
            result.run_id,
            {"error": str(exc), "source": "run_once_guard"},
        )
    except OSError as record_exc:
        LOGGER.error(
            "UNHANDLED_EXCEPTION_EVENT_PERSIST_FAILED: run_id=%s error=%s",
            result.run_id,
            record_exc,
        )

    try:
        checkpoint_store.append_run_result(result)
    except Exception as persist_exc:
        LOGGER.error(
            "UNHANDLED_EXCEPTION_RUN_RESULT_PERSIST_FAILED: run_id=%s error=%s",
            result.run_id,
            persist_exc,
        )

    _handle_incident_lifecycle(
        checkpoint_store=checkpoint_store,
        discord_webhook_url=runtime.discord_webhook_url,
        dry_run=runtime.dry_run,
        result=result,
        incident_memory_suppressed_until=_incident_memory_map(runtime),
    )
    return result
