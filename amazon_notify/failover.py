from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import LOGGER, load_state, save_state
from .discord_client import send_discord_alert, send_discord_recovery
from .time_utils import utc_now_iso

FAILOVER_ACTIVE_KEY = "pubsub_failover_active"
FAILOVER_REASON_KEY = "pubsub_failover_reason"
FAILOVER_AT_KEY = "pubsub_failover_at"
FAILOVER_SUPPRESSED_KEY = "pubsub_failover_suppressed_count"
_DISCORD_DEDUPE_STATE_FILENAME = ".discord_dedupe_state.json"


def _send_discord_alert_with_dedupe(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path,
) -> bool:
    try:
        return send_discord_alert(
            webhook_url,
            message,
            dedupe_state_path=dedupe_state_path,
        )
    except TypeError:
        return send_discord_alert(webhook_url, message)


def _send_discord_recovery_with_dedupe(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path,
) -> bool:
    try:
        return send_discord_recovery(
            webhook_url,
            message,
            dedupe_state_path=dedupe_state_path,
        )
    except TypeError:
        return send_discord_recovery(webhook_url, message)


@dataclass(frozen=True)
class MainHealthStatus:
    healthy: bool
    reason: str
    service_state: str | None
    heartbeat_age_seconds: float | None
    worker_heartbeat_age_seconds: float | None = None


def get_systemd_service_state(service_name: str) -> str | None:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service_name],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None

    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0:
        return "active"
    return output or "unknown"


def heartbeat_age_seconds(
    heartbeat_file: Path, now: float | None = None
) -> float | None:
    if not heartbeat_file.exists():
        return None
    current_time = time.time() if now is None else now
    snapshot = load_heartbeat_snapshot(heartbeat_file)
    if snapshot is not None:
        updated_at = snapshot.get("updated_at")
        if isinstance(updated_at, (int, float)):
            age = current_time - float(updated_at)
            return max(0.0, age)

    age = current_time - heartbeat_file.stat().st_mtime
    return max(0.0, age)


def worker_heartbeat_age_seconds(
    heartbeat_file: Path, now: float | None = None
) -> float | None:
    if not heartbeat_file.exists():
        return None
    snapshot = load_heartbeat_snapshot(heartbeat_file)
    if snapshot is None:
        return None
    worker_last_seen_at = snapshot.get("worker_last_seen_at")
    if not isinstance(worker_last_seen_at, (int, float)):
        return None
    current_time = time.time() if now is None else now
    age = current_time - float(worker_last_seen_at)
    return max(0.0, age)


def load_heartbeat_snapshot(heartbeat_file: Path) -> dict | None:
    try:
        raw = heartbeat_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def evaluate_main_health(
    *,
    service_name: str,
    heartbeat_file: Path,
    heartbeat_max_age_seconds: float,
) -> MainHealthStatus:
    service_state = get_systemd_service_state(service_name)
    age = heartbeat_age_seconds(heartbeat_file)
    worker_age = worker_heartbeat_age_seconds(heartbeat_file)

    if service_state is not None and service_state != "active":
        return MainHealthStatus(
            healthy=False,
            reason=f"service_not_active: service={service_name} state={service_state}",
            service_state=service_state,
            heartbeat_age_seconds=age,
            worker_heartbeat_age_seconds=worker_age,
        )

    if age is None:
        return MainHealthStatus(
            healthy=False,
            reason=f"heartbeat_missing: file={heartbeat_file}",
            service_state=service_state,
            heartbeat_age_seconds=None,
            worker_heartbeat_age_seconds=worker_age,
        )

    if age > heartbeat_max_age_seconds:
        return MainHealthStatus(
            healthy=False,
            reason=(
                f"heartbeat_stale: age={age:.1f}s "
                f"limit={heartbeat_max_age_seconds:.1f}s file={heartbeat_file}"
            ),
            service_state=service_state,
            heartbeat_age_seconds=age,
            worker_heartbeat_age_seconds=worker_age,
        )

    if worker_age is not None and worker_age > heartbeat_max_age_seconds:
        return MainHealthStatus(
            healthy=False,
            reason=(
                f"worker_heartbeat_stale: age={worker_age:.1f}s "
                f"limit={heartbeat_max_age_seconds:.1f}s file={heartbeat_file}"
            ),
            service_state=service_state,
            heartbeat_age_seconds=age,
            worker_heartbeat_age_seconds=worker_age,
        )

    if service_state is None:
        reason = (
            "service_state_unknown_but_heartbeat_fresh: "
            f"age={age:.1f}s limit={heartbeat_max_age_seconds:.1f}s"
        )
    else:
        reason = f"main_healthy: service={service_name} age={age:.1f}s"
    return MainHealthStatus(
        healthy=True,
        reason=reason,
        service_state=service_state,
        heartbeat_age_seconds=age,
        worker_heartbeat_age_seconds=worker_age,
    )


def evaluate_failover_watchdog(
    *,
    state_file: Path,
    discord_webhook_url: str,
    service_name: str,
    heartbeat_file: Path,
    heartbeat_max_age_seconds: float,
    dry_run: bool = False,
) -> bool:
    dedupe_state_path = state_file.parent / _DISCORD_DEDUPE_STATE_FILENAME
    health = evaluate_main_health(
        service_name=service_name,
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=heartbeat_max_age_seconds,
    )
    state = load_state(state_file)
    failover_active = bool(state.get(FAILOVER_ACTIVE_KEY))

    if health.healthy:
        LOGGER.info("FALLBACK_WATCHDOG_SKIP: %s", health.reason)
        if failover_active:
            if dry_run:
                return False
            if discord_webhook_url:
                sent = _send_discord_recovery_with_dedupe(
                    discord_webhook_url,
                    "Pub/Sub メイン系のヘルスが回復したため、フェールオーバー待機状態を解除しました。\n"
                    f"detail: {health.reason}",
                    dedupe_state_path=dedupe_state_path,
                )
                if not sent:
                    LOGGER.warning("FALLBACK_RECOVERY_ALERT_FAILED")
                    return False
            state.pop(FAILOVER_ACTIVE_KEY, None)
            state.pop(FAILOVER_REASON_KEY, None)
            state.pop(FAILOVER_AT_KEY, None)
            state.pop(FAILOVER_SUPPRESSED_KEY, None)
            save_state(state_file, state)
        return False

    LOGGER.warning("FALLBACK_WATCHDOG_FAILOVER: %s", health.reason)
    if not failover_active:
        if not dry_run and discord_webhook_url:
            sent = _send_discord_alert_with_dedupe(
                discord_webhook_url,
                "⚠️ Pub/Sub が停止または異常のため、ポーリングへフェールオーバーします。\n"
                f"detail: {health.reason}",
                dedupe_state_path=dedupe_state_path,
            )
            if not sent:
                LOGGER.warning("FALLBACK_FAILOVER_ALERT_FAILED")
        state[FAILOVER_ACTIVE_KEY] = True
        state[FAILOVER_REASON_KEY] = health.reason
        state[FAILOVER_AT_KEY] = utc_now_iso()
        state[FAILOVER_SUPPRESSED_KEY] = 0
        save_state(state_file, state)
        return True

    suppressed_count = int(state.get(FAILOVER_SUPPRESSED_KEY, 0)) + 1
    state[FAILOVER_SUPPRESSED_KEY] = suppressed_count
    state[FAILOVER_REASON_KEY] = health.reason
    save_state(state_file, state)
    return True
