from __future__ import annotations

from dataclasses import dataclass

from .domain import FailureKind


@dataclass
class PipelineError(Exception):
    message: str
    kind: FailureKind
    should_retry: bool
    should_alert: bool
    message_id: str | None = None

    def __str__(self) -> str:
        return self.message


class TransientSourceError(PipelineError):
    def __init__(self, message: str):
        super().__init__(
            message=message,
            kind=FailureKind.SOURCE_FAILED,
            should_retry=True,
            should_alert=False,
        )


class SourceError(PipelineError):
    def __init__(self, message: str):
        super().__init__(
            message=message,
            kind=FailureKind.SOURCE_FAILED,
            should_retry=False,
            should_alert=True,
        )


class PermanentAuthError(PipelineError):
    def __init__(self, message: str):
        super().__init__(
            message=message,
            kind=FailureKind.AUTH_FAILED,
            should_retry=False,
            should_alert=True,
        )


class MessageDecodeError(PipelineError):
    def __init__(self, message: str, message_id: str):
        super().__init__(
            message=message,
            kind=FailureKind.MESSAGE_DETAIL_FAILED,
            should_retry=True,
            should_alert=True,
            message_id=message_id,
        )


class DeliveryError(PipelineError):
    def __init__(self, message: str, message_id: str):
        super().__init__(
            message=message,
            kind=FailureKind.DELIVERY_FAILED,
            should_retry=True,
            should_alert=True,
            message_id=message_id,
        )


class CheckpointError(PipelineError):
    def __init__(self, message: str, message_id: str | None = None):
        super().__init__(
            message=message,
            kind=FailureKind.CHECKPOINT_FAILED,
            should_retry=False,
            should_alert=True,
            message_id=message_id,
        )


class ConfigError(PipelineError):
    def __init__(self, message: str):
        super().__init__(
            message=message,
            kind=FailureKind.CONFIG_FAILED,
            should_retry=False,
            should_alert=True,
        )
