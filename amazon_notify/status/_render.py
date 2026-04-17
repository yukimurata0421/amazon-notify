from __future__ import annotations

from typing import Any


def format_status_summary(report: dict[str, Any]) -> str:
    incident = report.get("incident") or {}
    incident_status = report.get("incident_status")
    incident_kind = incident.get("kind")
    suppressed_count = incident.get("suppressed_count")
    if incident_status == "suppressed":
        incident_text = (
            f"suppressed(kind={incident_kind}, count={suppressed_count})"
            if incident_kind
            else "suppressed"
        )
    elif incident_status == "open":
        incident_text = f"open(kind={incident_kind})" if incident_kind else "open"
    elif incident_status == "recovered":
        incident_text = (
            f"recovered(kind={incident_kind})" if incident_kind else "recovered"
        )
    else:
        incident_text = "none"

    consistency = report.get("consistency") or {}
    checkpoint_ok = bool(consistency.get("checkpoint_consistent"))
    run_summary_ok = bool(consistency.get("run_summary_consistent"))
    incident_ok = bool(consistency.get("incident_consistent"))

    lines = [
        f"status: {report.get('status')}",
        f"frontier: {_fmt_or_dash(report.get('frontier'))}",
        f"last_success_at: {_fmt_or_dash(report.get('last_success_at'))}",
        f"incident: {incident_text}",
        f"last_failure_kind: {_fmt_or_dash(report.get('last_failure_kind'))}",
        "consistency: "
        f"checkpoint={_ok_text(checkpoint_ok)}, "
        f"runs={_ok_text(run_summary_ok)}, "
        f"incident={_ok_text(incident_ok)}",
    ]
    return "\n".join(lines)


def format_metrics_plain(report: dict[str, Any]) -> str:
    cp = report.get("checkpoint") or {}
    rr = report.get("runs_recent") or {}
    inc = report.get("incident") or {}
    dd = report.get("dedupe") or {}
    lines = [
        f"generated_at: {report.get('generated_at')}",
        f"checkpoint_age_seconds: {cp.get('age_seconds')}",
        f"frontier_message_id: {_fmt_or_dash(cp.get('frontier_message_id'))}",
        f"runs_window: {rr.get('window_runs')} "
        f"ok={rr.get('success_count')} err={rr.get('failure_count')} "
        f"ratio={rr.get('failure_ratio')}",
        f"dedupe_entries: {dd.get('entry_count')}",
        f"incident: {inc.get('status')} duration_s={inc.get('open_duration_seconds')}",
    ]
    return "\n".join(lines)


def _fmt_or_dash(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, str) and not value:
        return "-"
    return str(value)


def _ok_text(value: bool) -> str:
    return "ok" if value else "degraded"
