from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..runtime import RuntimeConfig
from ..time_utils import utc_now_iso
from ._collect import (
    count_events_named,
    dedupe_metrics,
    last_checkpoint_advanced_at,
    load_runs_payloads,
    metrics_incident_open_duration,
    read_json_object,
    scan_events_jsonl,
)
from ._doctor import build_runtime_report


def build_status_report(runtime: RuntimeConfig) -> tuple[int, dict[str, Any]]:
    report = build_runtime_report(runtime)
    summary = {
        "status": report["status"],
        "checked_at": report["checked_at"],
        "frontier": report["frontier"],
        "last_success_at": report["last_success_at"],
        "incident_status": report["incident_status"],
        "incident": report["incident"],
        "last_failure_kind": report["last_failure_kind"],
        "consistency": {
            "checkpoint_consistent": report["consistency"]["checkpoint_consistent"],
            "run_summary_consistent": report["consistency"]["run_summary_consistent"],
            "incident_consistent": report["consistency"]["incident_consistent"],
        },
    }
    return (0 if summary["status"] == "ok" else 1), summary


def build_doctor_report(runtime: RuntimeConfig) -> tuple[int, dict[str, Any]]:
    report = build_runtime_report(runtime)
    return (0 if report["status"] == "ok" else 1), report


def build_metrics_report(
    runtime: RuntimeConfig,
    *,
    recent_run_window: int = 50,
) -> dict[str, Any]:
    """Thin operational metrics for external monitors (JSON-friendly)."""
    now = datetime.now(UTC)
    events_scan = scan_events_jsonl(runtime.events_file)
    runs_payloads = load_runs_payloads(runtime.runs_file)

    last_cp_at = last_checkpoint_advanced_at(runtime.events_file)
    checkpoint_age_seconds: float | None = None
    if last_cp_at is not None:
        checkpoint_age_seconds = max(0.0, (now - last_cp_at).total_seconds())

    window_cap = max(1, recent_run_window)
    window = min(window_cap, len(runs_payloads)) if runs_payloads else 0
    recent = runs_payloads[-window:] if window else []

    ok_runs = sum(1 for r in recent if r.get("failure_kind") in (None, ""))
    err_runs = len(recent) - ok_runs
    failure_ratio = (err_runs / len(recent)) if recent else 0.0
    notified_sum = sum(
        int(r.get("notified_count") or 0)
        for r in recent
        if r.get("failure_kind") in (None, "")
    )

    incident_suppressed_total = count_events_named(
        runtime.events_file, "incident_suppressed"
    )

    dedupe_info = dedupe_metrics(runtime.discord_dedupe_state_file)
    state_payload, _state_err, _state_exists = read_json_object(runtime.state_file)

    frontier = events_scan["last_checkpoint"]
    last_run = runs_payloads[-1] if runs_payloads else None

    return {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "checkpoint": {
            "frontier_message_id": frontier,
            "last_advanced_at": last_cp_at.isoformat() if last_cp_at else None,
            "age_seconds": checkpoint_age_seconds,
        },
        "runs_recent": {
            "window_runs": window,
            "success_count": ok_runs,
            "failure_count": err_runs,
            "failure_ratio": round(failure_ratio, 4),
            "notified_total_in_window": notified_sum,
        },
        "incident_events": {
            "suppressed_total": incident_suppressed_total,
        },
        "dedupe": dedupe_info,
        "incident": metrics_incident_open_duration(state_payload, now),
        "last_run": {
            "run_id": last_run.get("run_id") if last_run else None,
            "ended_at": last_run.get("ended_at") if last_run else None,
            "failure_kind": last_run.get("failure_kind") if last_run else None,
        },
    }
