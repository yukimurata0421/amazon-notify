from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import LOGGER, save_state
from .failover import load_heartbeat_snapshot


@dataclass
class ServiceStatusTracker:
    status_file: Path
    heartbeat_file: Path
    service: str = "amazon-notify"

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.internal_state = "degraded"
        self.reason = "initializing"
        self.last_success_ts: str | None = None
        self.last_progress_ts: str | None = None

        self.consecutive_failures = 0
        self.last_recovery_action = "none"
        self.last_recovery_ts: str | None = None

        self.pubsub_status = "stalled"
        self.gmail_last_success_ts: str | None = None
        self.discord_last_success_ts: str | None = None
        self.checkpoint_last_advanced_ts: str | None = None

    def mark_trigger_result(self, ok: bool, *, reason: str) -> None:
        now_iso = _utc_now_iso()
        with self._lock:
            self.last_progress_ts = now_iso
            if ok:
                self.last_success_ts = now_iso
                self.gmail_last_success_ts = now_iso
                self.discord_last_success_ts = now_iso
                self.checkpoint_last_advanced_ts = now_iso
                self.internal_state = (
                    "healthy" if self.consecutive_failures == 0 else "degraded"
                )
                self.reason = "running"
                self.pubsub_status = "healthy"
                self.consecutive_failures = 0
                self.last_recovery_action = "none"
                self.last_recovery_ts = None
            else:
                self.internal_state = "degraded"
                self.reason = reason
                self.pubsub_status = "stalled"

        self.write()

    def mark_reconnect_attempt(self, *, action: str, reason: str) -> None:
        now_iso = _utc_now_iso()
        with self._lock:
            self.internal_state = "degraded"
            self.reason = reason
            self.pubsub_status = "stalled"
            self.last_progress_ts = now_iso
            self.consecutive_failures += 1
            self.last_recovery_action = action
            self.last_recovery_ts = now_iso
        self.write()

    def mark_failed(self, *, reason: str) -> None:
        now_iso = _utc_now_iso()
        with self._lock:
            self.internal_state = "failed"
            self.reason = reason
            self.pubsub_status = "failed"
            self.last_progress_ts = now_iso
            self.last_recovery_action = "fail_fast"
            self.last_recovery_ts = now_iso
        self.write()

    def mark_system_progress(self, *, reason: str = "heartbeat_tick") -> None:
        now_iso = _utc_now_iso()
        with self._lock:
            self.last_progress_ts = now_iso
            if self.internal_state == "healthy":
                self.reason = reason
        self.write()

    def write(self) -> None:
        payload = self._build_payload()
        try:
            save_state(self.status_file, payload)
        except OSError as exc:
            LOGGER.warning(
                "SERVICE_STATUS_WRITE_FAILED: file=%s error=%s",
                self.status_file,
                exc,
            )

    def _build_payload(self) -> dict[str, Any]:
        with self._lock:
            internal_state = self.internal_state
            reason = self.reason
            last_success_ts = self.last_success_ts
            last_progress_ts = self.last_progress_ts
            consecutive_failures = self.consecutive_failures
            last_recovery_action = self.last_recovery_action
            last_recovery_ts = self.last_recovery_ts
            pubsub_status = self.pubsub_status
            gmail_last_success_ts = self.gmail_last_success_ts
            discord_last_success_ts = self.discord_last_success_ts
            checkpoint_last_advanced_ts = self.checkpoint_last_advanced_ts

        return {
            "schema_version": 1,
            "service": self.service,
            "updated_at": _utc_now_iso(),
            "internal_state": internal_state,
            "reason": reason,
            "last_success_ts": last_success_ts,
            "last_progress_ts": last_progress_ts,
            "recovery": {
                "consecutive_failures": consecutive_failures,
                "last_recovery_action": last_recovery_action,
                "last_recovery_ts": last_recovery_ts,
            },
            "components": {
                "pubsub": {
                    "status": pubsub_status,
                    "last_heartbeat_ts": _read_pubsub_last_heartbeat_iso(
                        self.heartbeat_file
                    ),
                },
                "gmail": {"last_success_ts": gmail_last_success_ts},
                "discord": {"last_success_ts": discord_last_success_ts},
                "checkpoint": {"last_advanced_ts": checkpoint_last_advanced_ts},
            },
        }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_pubsub_last_heartbeat_iso(heartbeat_file: Path) -> str | None:
    snapshot = load_heartbeat_snapshot(heartbeat_file)
    if snapshot is None:
        return None

    updated_at = snapshot.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return None

    try:
        return datetime.fromtimestamp(float(updated_at), UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
