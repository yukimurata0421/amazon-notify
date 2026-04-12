from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runtime import RuntimeConfig
from .time_utils import utc_now_iso


def build_metrics_report(runtime: RuntimeConfig, *, window: int = 20) -> dict[str, Any]:
    window_size = max(1, int(window))

    runs = _read_jsonl_rows(runtime.runs_file)
    events = _read_jsonl_rows(runtime.events_file)
    state_payload = _read_json_object(runtime.state_file)

    recent_runs = runs[-window_size:] if runs else []
    failure_count = sum(1 for row in recent_runs if row.get("failure_kind") not in (None, ""))
    success_count = len(recent_runs) - failure_count
    failure_ratio = (failure_count / len(recent_runs)) if recent_runs else 0.0

    notified_count = sum(_as_int(row.get("notified_count")) for row in recent_runs)
    suppressed_count = sum(1 for row in events if row.get("event") == "incident_suppressed")
    dedupe_hit_count = sum(1 for row in events if row.get("event") == "incident_suppressed")

    checkpoint_age_seconds = _checkpoint_age_seconds(events)
    open_incident_duration_seconds = _open_incident_duration_seconds(state_payload)

    metrics = {
        "checked_at": utc_now_iso(),
        "window_size": window_size,
        "runs_in_window": len(recent_runs),
        "checkpoint_age_seconds": checkpoint_age_seconds,
        "notifications_sent": notified_count,
        "run_success_count": success_count,
        "run_failure_count": failure_count,
        "run_failure_ratio": round(failure_ratio, 4),
        "incident_suppressed_count": suppressed_count,
        "dedupe_hit_count": dedupe_hit_count,
        "open_incident_duration_seconds": open_incident_duration_seconds,
    }
    return metrics


def format_metrics_plain(metrics: dict[str, Any]) -> str:
    lines = [
        f"checked_at={metrics.get('checked_at')}",
        f"window_size={metrics.get('window_size')}",
        f"runs_in_window={metrics.get('runs_in_window')}",
        f"checkpoint_age_seconds={_fmt_num(metrics.get('checkpoint_age_seconds'))}",
        f"notifications_sent={_fmt_num(metrics.get('notifications_sent'))}",
        f"run_success_count={_fmt_num(metrics.get('run_success_count'))}",
        f"run_failure_count={_fmt_num(metrics.get('run_failure_count'))}",
        f"run_failure_ratio={_fmt_num(metrics.get('run_failure_ratio'))}",
        f"incident_suppressed_count={_fmt_num(metrics.get('incident_suppressed_count'))}",
        f"dedupe_hit_count={_fmt_num(metrics.get('dedupe_hit_count'))}",
        "open_incident_duration_seconds="
        f"{_fmt_num(metrics.get('open_incident_duration_seconds'))}",
    ]
    return "\n".join(lines)


def _fmt_num(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if not value.is_integer() else str(int(value))
    return str(value)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            while True:
                raw = handle.readline()
                if raw == b"":
                    break
                is_tail = handle.tell() == size
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if is_tail:
                        continue
                    return rows
                if isinstance(payload, dict):
                    rows.append(payload)
    except OSError:
        return rows

    return rows


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _checkpoint_age_seconds(events: list[dict[str, Any]]) -> float | None:
    checkpoint_at: datetime | None = None
    for row in events:
        if row.get("event") != "checkpoint_advanced":
            continue
        parsed = _parse_ts(row.get("at"))
        if parsed is not None:
            checkpoint_at = parsed

    if checkpoint_at is None:
        return None
    now = datetime.now(UTC)
    age = (now - checkpoint_at).total_seconds()
    return max(age, 0.0)


def _open_incident_duration_seconds(state_payload: dict[str, Any] | None) -> float | None:
    if not isinstance(state_payload, dict):
        return None
    kind = state_payload.get("active_incident_kind")
    if not isinstance(kind, str) or not kind:
        return None
    opened_at = _parse_ts(state_payload.get("active_incident_at"))
    if opened_at is None:
        return None
    now = datetime.now(UTC)
    age = (now - opened_at).total_seconds()
    return max(age, 0.0)
