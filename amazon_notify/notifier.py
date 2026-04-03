from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from re import Pattern

from .checkpoint_store import JsonlCheckpointStore
from .config import LOGGER, load_state, save_state
from .discord_client import send_discord_alert, send_discord_notification, send_discord_recovery
from .domain import AuthStatus, Checkpoint, MailEnvelope, NotificationCandidate, RunResult
from .errors import MessageDecodeError, PermanentAuthError, SourceError, TransientSourceError
from .gmail_client import (
    HttpError,
    get_gmail_service,
    get_last_auth_status,
    get_message_detail,
    is_transient_network_error,
    list_recent_messages,
    mark_transient_network_issue,
    notify_recovery_if_needed,
)
from .pipeline import NotificationPipeline
from .text import build_gmail_message_url, decode_mime_words, extract_email_address, is_amazon_mail


@dataclass
class GmailMailSource:
    discord_webhook_url: str
    state: dict
    state_file: Path
    dry_run: bool

    def get_auth_status(self) -> AuthStatus:
        return get_last_auth_status()

    def notify_recovery_if_needed(self) -> None:
        if self.dry_run:
            return
        notify_recovery_if_needed(self.discord_webhook_url, self.state, self.state_file)

    def mark_transient_issue(self, err: Exception | str) -> None:
        if self.dry_run:
            return
        mark_transient_network_issue(self.state, self.state_file, err)

    def iter_new_messages(self, checkpoint: Checkpoint, max_messages: int) -> Iterable[MailEnvelope]:
        service = get_gmail_service(
            webhook_url=None,  # alert は incident lifecycle 側で一元管理する
            state=None if self.dry_run else self.state,
            state_file=None if self.dry_run else self.state_file,
        )
        if service is None:
            auth_status = self.get_auth_status()
            if auth_status in {
                AuthStatus.REFRESH_TRANSIENT_FAILURE,
                AuthStatus.SERVICE_BUILD_TRANSIENT_FAILURE,
            }:
                raise TransientSourceError(
                    f"Gmail service 一時障害: auth_status={auth_status.value}"
                )
            raise PermanentAuthError(
                f"Gmail service が利用できません。auth_status={auth_status.value}"
            )

        try:
            messages = list_recent_messages(service, query="in:inbox", max_results=max_messages)
        except Exception as exc:
            if isinstance(exc, HttpError):
                raise TransientSourceError(f"Gmail API 呼び出しエラー: {exc}") from exc
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
                msg = get_message_detail(service, msg_id)
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
    amazon_pattern: str
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
        )


def _handle_incident_lifecycle(
    *,
    checkpoint_store: JsonlCheckpointStore,
    state: dict,
    state_file: Path,
    discord_webhook_url: str,
    dry_run: bool,
    result: RunResult,
) -> None:
    active_kind = state.get("active_incident_kind")
    failure_kind = result.failure_kind.value if result.failure_kind else None

    if result.failure_kind is not None and result.should_alert and not dry_run and discord_webhook_url:
        # 同一インシデント継続時は抑制して連投を避ける。
        if active_kind == failure_kind:
            state["incident_suppressed_count"] = int(state.get("incident_suppressed_count", 0)) + 1
            save_state(state_file, state)
            checkpoint_store.append_event(
                "incident_suppressed",
                result.run_id,
                {
                    "kind": failure_kind,
                    "suppressed_count": state["incident_suppressed_count"],
                },
            )
            return

        message = result.failure_message or failure_kind or "unknown failure"
        if result.failure_message_id:
            message = f"{message}\nmessage_id: {result.failure_message_id}"
        sent = send_discord_alert(discord_webhook_url, message)
        if sent:
            state["active_incident_kind"] = failure_kind
            state["active_incident_message"] = result.failure_message
            state["active_incident_at"] = result.ended_at
            state["incident_suppressed_count"] = 0
            save_state(state_file, state)
            checkpoint_store.append_event(
                "incident_opened",
                result.run_id,
                {
                    "kind": failure_kind,
                },
            )
        return

    # 正常化したら close 通知して incident を解消する。
    if result.failure_kind is None and active_kind and not dry_run and discord_webhook_url:
        recovery_msg = (
            "障害状態から復旧しました。\n"
            f"kind: {active_kind}\n"
            f"suppressed_count: {state.get('incident_suppressed_count', 0)}"
        )
        sent = send_discord_recovery(discord_webhook_url, recovery_msg)
        if sent:
            checkpoint_store.append_event(
                "incident_recovered",
                result.run_id,
                {
                    "kind": active_kind,
                },
            )
            state.pop("active_incident_kind", None)
            state.pop("active_incident_message", None)
            state.pop("active_incident_at", None)
            state.pop("incident_suppressed_count", None)
            save_state(state_file, state)


def run_once(runtime: dict) -> RunResult:
    discord_webhook_url = runtime["discord_webhook_url"]
    amazon_pattern = runtime["amazon_pattern"]
    state_file: Path = runtime["state_file"]
    max_messages = runtime["max_messages"]
    subject_pattern: Pattern[str] | None = runtime["subject_pattern"]
    dry_run = bool(runtime.get("dry_run", False))
    events_file: Path | None = runtime.get("events_file")
    runs_file: Path | None = runtime.get("runs_file")

    state = load_state(state_file)
    LOGGER.info("RUN_ONCE_START: last_message_id=%s dry_run=%s", state.get("last_message_id"), dry_run)

    source = GmailMailSource(
        discord_webhook_url=discord_webhook_url,
        state=state,
        state_file=state_file,
        dry_run=dry_run,
    )
    classifier = RegexClassifier(
        amazon_pattern=amazon_pattern,
        subject_pattern=subject_pattern,
    )
    notifier = DiscordNotifier(
        webhook_url=discord_webhook_url,
        dry_run=dry_run,
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
        state=state,
        state_file=state_file,
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
