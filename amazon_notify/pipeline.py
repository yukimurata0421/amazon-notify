from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from .config import LOGGER
from .domain import (
    AuthStatus,
    Checkpoint,
    CheckpointStore,
    Classifier,
    FailureKind,
    MailSource,
    Notifier,
    RunResult,
)
from .errors import (
    CheckpointError,
    DeliveryError,
    PermanentAuthError,
    PipelineError,
    TransientSourceError,
)
from .time_utils import utc_now_iso


@dataclass(slots=True)
class _RunState:
    checkpoint_before: Checkpoint
    checkpoint_after: Checkpoint
    processed_count: int = 0
    matched_count: int = 0
    notified_count: int = 0
    non_target_count: int = 0
    failure_kind: FailureKind | None = None
    failure_message: str | None = None
    failure_message_id: str | None = None
    should_retry: bool = False
    should_alert: bool = False
    auth_status: AuthStatus | None = None


class NotificationPipeline:
    def __init__(
        self,
        source: MailSource,
        classifier: Classifier,
        notifier: Notifier,
        checkpoint_store: CheckpointStore,
        *,
        max_messages: int,
        dry_run: bool = False,
    ):
        self.source = source
        self.classifier = classifier
        self.notifier = notifier
        self.checkpoint_store = checkpoint_store
        self.max_messages = max_messages
        self.dry_run = dry_run

    def run_once(self) -> RunResult:
        run_id = uuid.uuid4().hex
        started_at = utc_now_iso()
        checkpoint_before = self.checkpoint_store.load_checkpoint()
        state = _RunState(
            checkpoint_before=checkpoint_before,
            checkpoint_after=checkpoint_before,
            auth_status=self.source.get_auth_status(),
        )

        try:
            self.source.notify_recovery_if_needed()
            for envelope in self.source.iter_new_messages(
                checkpoint_before, self.max_messages
            ):
                self._mark_message_processed(state=state)
                candidate = self.classifier.classify(envelope)
                if candidate is None:
                    state.non_target_count += 1
                    self._commit_checkpoint(
                        run_id=run_id,
                        state=state,
                        message_id=envelope.message_id,
                    )
                    continue

                state.matched_count += 1
                sent = self.notifier.notify(candidate)
                if not sent:
                    raise DeliveryError(
                        "Amazon メールの Discord 通知に失敗しました。",
                        envelope.message_id,
                    )
                state.notified_count += 1
                self._commit_checkpoint(
                    run_id=run_id,
                    state=state,
                    message_id=envelope.message_id,
                )
        except PipelineError as exc:
            self._handle_pipeline_error(run_id=run_id, state=state, exc=exc)
        except Exception as exc:
            self._handle_unexpected_error(run_id=run_id, state=state, exc=exc)

        result = self._build_run_result(
            run_id=run_id, started_at=started_at, state=state
        )
        result = self._persist_run_result(run_id=run_id, result=result)
        self._log_result(result)
        return result

    def _mark_message_processed(self, *, state: _RunState) -> None:
        state.processed_count += 1

    def _commit_checkpoint(
        self, *, run_id: str, state: _RunState, message_id: str
    ) -> None:
        state.checkpoint_after = self._commit_if_needed(
            run_id=run_id,
            checkpoint=Checkpoint(message_id=message_id),
            dry_run=self.dry_run,
        )

    def _handle_pipeline_error(
        self, *, run_id: str, state: _RunState, exc: PipelineError
    ) -> None:
        state.failure_kind = exc.kind
        state.failure_message = str(exc)
        state.failure_message_id = exc.message_id
        state.should_retry = exc.should_retry
        state.should_alert = exc.should_alert
        self._record_failure(run_id, exc)
        if isinstance(exc, TransientSourceError):
            self.source.mark_transient_issue(exc)
        if isinstance(exc, PermanentAuthError):
            state.auth_status = self.source.get_auth_status()

    def _handle_unexpected_error(
        self, *, run_id: str, state: _RunState, exc: Exception
    ) -> None:
        state.failure_kind = FailureKind.SOURCE_FAILED
        state.failure_message = str(exc)
        state.should_retry = True
        state.should_alert = True
        try:
            self.checkpoint_store.append_event(
                "source_failed",
                run_id,
                {"error": str(exc)},
            )
        except OSError as record_exc:
            LOGGER.error(
                "SOURCE_FAILURE_EVENT_PERSIST_FAILED: run_id=%s error=%s",
                run_id,
                record_exc,
            )
        self.source.mark_transient_issue(exc)

    def _build_run_result(
        self, *, run_id: str, started_at: str, state: _RunState
    ) -> RunResult:
        return RunResult(
            run_id=run_id,
            started_at=started_at,
            ended_at=utc_now_iso(),
            checkpoint_before=state.checkpoint_before.message_id,
            checkpoint_after=state.checkpoint_after.message_id,
            processed_count=state.processed_count,
            matched_count=state.matched_count,
            notified_count=state.notified_count,
            non_target_count=state.non_target_count,
            failure_kind=state.failure_kind,
            failure_message=state.failure_message,
            failure_message_id=state.failure_message_id,
            should_retry=state.should_retry,
            should_alert=state.should_alert,
            auth_status=state.auth_status,
        )

    def _persist_run_result(self, *, run_id: str, result: RunResult) -> RunResult:
        try:
            self.checkpoint_store.append_run_result(result)
        except CheckpointError as exc:
            LOGGER.error("RUN_RESULT_PERSIST_FAILED: run_id=%s error=%s", run_id, exc)
            return replace(
                result,
                failure_kind=FailureKind.CHECKPOINT_FAILED,
                failure_message=str(exc),
                failure_message_id=exc.message_id,
                should_retry=False,
                should_alert=True,
            )
        return result

    def _commit_if_needed(
        self, *, run_id: str, checkpoint: Checkpoint, dry_run: bool
    ) -> Checkpoint:
        if dry_run:
            return checkpoint
        try:
            self.checkpoint_store.advance_checkpoint(checkpoint, run_id)
            return checkpoint
        except CheckpointError as exc:
            self._record_failure(run_id, exc)
            raise

    def _record_failure(self, run_id: str, exc: PipelineError) -> None:
        event_type = {
            FailureKind.DELIVERY_FAILED: "delivery_failed",
            FailureKind.MESSAGE_DETAIL_FAILED: "message_detail_failed",
            FailureKind.AUTH_FAILED: "auth_failed",
            FailureKind.CHECKPOINT_FAILED: "checkpoint_failed",
            FailureKind.SOURCE_FAILED: "source_failed",
            FailureKind.CONFIG_FAILED: "config_failed",
        }.get(exc.kind, "source_failed")
        payload = {"error": str(exc)}
        if exc.message_id:
            payload["message_id"] = exc.message_id
        try:
            self.checkpoint_store.append_event(event_type, run_id, payload)
        except OSError as record_exc:
            LOGGER.error(
                "FAILURE_EVENT_PERSIST_FAILED: run_id=%s event=%s error=%s",
                run_id,
                event_type,
                record_exc,
            )

    def _log_result(self, result: RunResult) -> None:
        LOGGER.info(
            "RUN_RESULT: run_id=%s processed=%s matched=%s notified=%s non_target=%s "
            "checkpoint_before=%s checkpoint_after=%s failure_kind=%s should_retry=%s",
            result.run_id,
            result.processed_count,
            result.matched_count,
            result.notified_count,
            result.non_target_count,
            result.checkpoint_before,
            result.checkpoint_after,
            result.failure_kind.value if result.failure_kind else None,
            result.should_retry,
        )
