from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable, Protocol


class FailureKind(str, Enum):
    SOURCE_FAILED = "source_failed"
    AUTH_FAILED = "auth_failed"
    MESSAGE_DETAIL_FAILED = "message_detail_failed"
    DELIVERY_FAILED = "delivery_failed"
    CHECKPOINT_FAILED = "checkpoint_failed"
    CONFIG_FAILED = "config_failed"


class AuthStatus(str, Enum):
    TOKEN_MISSING = "TOKEN_MISSING"
    TOKEN_CORRUPTED = "TOKEN_CORRUPTED"
    TOKEN_VALID = "TOKEN_VALID"
    TOKEN_EXPIRED_REFRESHABLE = "TOKEN_EXPIRED_REFRESHABLE"
    REFRESH_TRANSIENT_FAILURE = "REFRESH_TRANSIENT_FAILURE"
    REFRESH_PERMANENT_FAILURE = "REFRESH_PERMANENT_FAILURE"
    INTERACTIVE_REAUTH_REQUIRED = "INTERACTIVE_REAUTH_REQUIRED"
    SERVICE_BUILD_TRANSIENT_FAILURE = "SERVICE_BUILD_TRANSIENT_FAILURE"
    READY = "READY"


@dataclass(frozen=True)
class Checkpoint:
    message_id: str | None


@dataclass(frozen=True)
class MailEnvelope:
    message_id: str
    subject: str
    from_header: str
    snippet: str


@dataclass(frozen=True)
class NotificationCandidate:
    envelope: MailEnvelope
    from_addr: str
    url: str


@dataclass(frozen=True)
class RunResult:
    run_id: str
    started_at: str
    ended_at: str
    checkpoint_before: str | None
    checkpoint_after: str | None
    processed_count: int
    matched_count: int
    notified_count: int
    non_target_count: int
    failure_kind: FailureKind | None
    failure_message: str | None
    failure_message_id: str | None
    should_retry: bool
    should_alert: bool
    auth_status: AuthStatus | None

    def to_json_dict(self) -> dict:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else None
        payload["auth_status"] = self.auth_status.value if self.auth_status else None
        return payload


class MailSource(Protocol):
    def get_auth_status(self) -> AuthStatus: ...

    def notify_recovery_if_needed(self) -> None: ...

    def mark_transient_issue(self, err: Exception | str) -> None: ...

    def iter_new_messages(self, checkpoint: Checkpoint, max_messages: int) -> Iterable[MailEnvelope]: ...


class Notifier(Protocol):
    def notify(self, candidate: NotificationCandidate) -> bool: ...


class Classifier(Protocol):
    def classify(self, envelope: MailEnvelope) -> NotificationCandidate | None: ...


class CheckpointStore(Protocol):
    def load_checkpoint(self) -> Checkpoint: ...

    def advance_checkpoint(self, checkpoint: Checkpoint, run_id: str) -> None: ...

    def append_event(self, event_type: str, run_id: str, payload: dict) -> None: ...

    def append_run_result(self, result: RunResult) -> None: ...
