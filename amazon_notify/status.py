from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runtime import RuntimeConfig
from .time_utils import parse_utc_iso, utc_now_iso

_SUMMARY_KEYS = (
    "last_run_status",
    "last_failure_kind",
    "checkpoint_before",
    "checkpoint_after",
    "auth_status",
    "last_success_at",
)
_INCIDENT_EVENT_TO_STATUS = {
    "incident_opened": "open",
    "incident_suppressed": "suppressed",
    "incident_recovered": "recovered",
}


def build_status_report(runtime: RuntimeConfig) -> tuple[int, dict[str, Any]]:
    report = _build_runtime_report(runtime)
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
    report = _build_runtime_report(runtime)
    return (0 if report["status"] == "ok" else 1), report


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


def _fmt_or_dash(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, str) and not value:
        return "-"
    return str(value)


def _ok_text(value: bool) -> str:
    return "ok" if value else "degraded"


def _build_readability_checks(
    runtime: RuntimeConfig,
    *,
    state_error: str | None,
    state_exists: bool,
    events_scan: dict[str, Any],
    runs_scan: dict[str, Any],
) -> list[dict[str, str | bool]]:
    return [
        {
            "name": "state_json_readable",
            "ok": state_error is None,
            "detail": _readable_detail(runtime.state_file, state_exists, state_error),
        },
        {
            "name": "events_jsonl_readable",
            "ok": events_scan["error"] is None,
            "detail": _jsonl_detail(runtime.events_file, events_scan),
        },
        {
            "name": "runs_jsonl_readable",
            "ok": runs_scan["error"] is None,
            "detail": _jsonl_detail(runtime.runs_file, runs_scan),
        },
    ]


def _build_consistency_checks(
    runtime: RuntimeConfig,
    *,
    state_payload: dict[str, Any] | None,
    state_error: str | None,
    events_scan: dict[str, Any],
    runs_scan: dict[str, Any],
    state_last_message_id: str | None,
    runs_state_summary: dict[str, Any] | None,
    checkpoint_index_payload: dict[str, Any] | None,
    checkpoint_index_error: str | None,
    checkpoint_index_exists: bool,
    runs_index_payload: dict[str, Any] | None,
    runs_index_error: str | None,
    runs_index_exists: bool,
) -> list[dict[str, str | bool]]:
    checks: list[dict[str, str | bool]] = []

    checkpoint_ok, checkpoint_detail = _check_checkpoint_consistency(
        events_frontier=events_scan["last_checkpoint"],
        state_checkpoint=state_last_message_id,
        events_scan_error=events_scan["error"],
    )
    checks.append(
        {
            "name": "checkpoint_state_consistent",
            "ok": checkpoint_ok,
            "detail": checkpoint_detail,
        }
    )

    checkpoint_index_ok, checkpoint_index_detail = _check_checkpoint_index_consistency(
        events_file=runtime.events_file,
        events_frontier=events_scan["last_checkpoint"],
        checkpoint_index_payload=checkpoint_index_payload,
        checkpoint_index_error=checkpoint_index_error,
        checkpoint_index_exists=checkpoint_index_exists,
        events_scan_error=events_scan["error"],
    )
    checks.append(
        {
            "name": "checkpoint_index_consistent",
            "ok": checkpoint_index_ok,
            "detail": checkpoint_index_detail,
        }
    )

    runs_index_summary = _normalize_summary(
        runs_index_payload.get("summary")
        if isinstance(runs_index_payload, dict)
        else None
    )
    runs_summary = runs_scan["summary"]
    run_summary_ok, run_summary_detail = _check_run_summary_consistency(
        runs_summary=runs_summary,
        state_summary=runs_state_summary,
        runs_scan_error=runs_scan["error"],
    )
    checks.append(
        {
            "name": "run_summary_state_consistent",
            "ok": run_summary_ok,
            "detail": run_summary_detail,
        }
    )

    runs_index_ok, runs_index_detail = _check_run_summary_index_consistency(
        runs_file=runtime.runs_file,
        runs_summary=runs_summary,
        runs_index_payload=runs_index_payload,
        runs_index_summary=runs_index_summary,
        runs_index_error=runs_index_error,
        runs_index_exists=runs_index_exists,
        runs_scan_error=runs_scan["error"],
    )
    checks.append(
        {
            "name": "run_summary_index_consistent",
            "ok": runs_index_ok,
            "detail": runs_index_detail,
        }
    )

    incident_ok, incident_detail = _check_incident_consistency(
        state_payload=state_payload,
        latest_incident_event=events_scan["last_incident_event"],
        events_scan_error=events_scan["error"],
        state_error=state_error,
    )
    checks.append(
        {
            "name": "incident_lifecycle_consistent",
            "ok": incident_ok,
            "detail": incident_detail,
        }
    )

    return checks


def _build_runtime_report(runtime: RuntimeConfig) -> dict[str, Any]:
    state_payload, state_error, state_exists = _read_json_object(runtime.state_file)
    events_scan = _scan_events_jsonl(runtime.events_file)
    runs_scan = _scan_runs_jsonl(runtime.runs_file)

    checkpoint_index_payload, checkpoint_index_error, checkpoint_index_exists = (
        _read_json_object(
            runtime.events_file.with_name(
                f"{runtime.events_file.name}.checkpoint.index.json"
            )
        )
    )
    runs_index_payload, runs_index_error, runs_index_exists = _read_json_object(
        runtime.runs_file.with_name(f"{runtime.runs_file.name}.summary.index.json")
    )

    state_last_message_id = _state_last_message_id(state_payload)
    runs_state_summary = _normalize_summary(
        state_payload.get("last_run_summary")
        if isinstance(state_payload, dict)
        else None
    )

    frontier = events_scan["last_checkpoint"]
    if frontier is None and state_last_message_id is not None:
        frontier = state_last_message_id

    runs_summary = runs_scan["summary"]
    last_success_at = runs_summary.get("last_success_at") if runs_summary else None
    last_failure_kind = runs_summary.get("last_failure_kind") if runs_summary else None

    incident, incident_status = _derive_incident_status(
        state_payload=state_payload,
        latest_incident_event=events_scan["last_incident_event"],
    )

    readability_checks = _build_readability_checks(
        runtime,
        state_error=state_error,
        state_exists=state_exists,
        events_scan=events_scan,
        runs_scan=runs_scan,
    )
    consistency_checks = _build_consistency_checks(
        runtime,
        state_payload=state_payload,
        state_error=state_error,
        events_scan=events_scan,
        runs_scan=runs_scan,
        state_last_message_id=state_last_message_id,
        runs_state_summary=runs_state_summary,
        checkpoint_index_payload=checkpoint_index_payload,
        checkpoint_index_error=checkpoint_index_error,
        checkpoint_index_exists=checkpoint_index_exists,
        runs_index_payload=runs_index_payload,
        runs_index_error=runs_index_error,
        runs_index_exists=runs_index_exists,
    )
    checks = readability_checks + consistency_checks

    checkpoint_ok = all(
        bool(c["ok"]) for c in consistency_checks if "checkpoint" in str(c["name"])
    )
    run_summary_ok = all(
        bool(c["ok"]) for c in consistency_checks if "run_summary" in str(c["name"])
    )
    incident_ok = all(
        bool(c["ok"]) for c in consistency_checks if "incident" in str(c["name"])
    )

    runs_index_summary = _normalize_summary(
        runs_index_payload.get("summary")
        if isinstance(runs_index_payload, dict)
        else None
    )

    overall_status = "ok" if all(bool(check["ok"]) for check in checks) else "degraded"

    return {
        "status": overall_status,
        "checked_at": utc_now_iso(),
        "runtime_files": {
            "state_file": str(runtime.state_file),
            "events_file": str(runtime.events_file),
            "runs_file": str(runtime.runs_file),
            "events_checkpoint_index_file": str(
                runtime.events_file.with_name(
                    f"{runtime.events_file.name}.checkpoint.index.json"
                )
            ),
            "runs_summary_index_file": str(
                runtime.runs_file.with_name(
                    f"{runtime.runs_file.name}.summary.index.json"
                )
            ),
        },
        "frontier": frontier,
        "last_success_at": last_success_at,
        "last_failure_kind": last_failure_kind,
        "incident_status": incident_status,
        "incident": incident,
        "consistency": {
            "checkpoint_consistent": checkpoint_ok,
            "run_summary_consistent": run_summary_ok,
            "incident_consistent": incident_ok,
        },
        "runtime_status": {
            "events_frontier": events_scan["last_checkpoint"],
            "state_checkpoint": state_last_message_id,
            "checkpoint_index": _checkpoint_value_from_index(checkpoint_index_payload),
            "runs_summary_from_runs": runs_summary,
            "runs_summary_from_state": runs_state_summary,
            "runs_summary_from_index": runs_index_summary,
            "latest_incident_event": events_scan["last_incident_event"],
            "active_incident_state": _active_incident_from_state(state_payload),
            "tail_corruption_ignored": {
                "events_jsonl": bool(events_scan["tail_corruption_ignored"]),
                "runs_jsonl": bool(runs_scan["tail_corruption_ignored"]),
            },
        },
        "checks": checks,
    }


def _readable_detail(path: Path, exists: bool, error: str | None) -> str:
    if error is not None:
        return error
    if not exists:
        return f"missing: {path}"
    return f"ok: {path}"


def _jsonl_detail(path: Path, scan: dict[str, Any]) -> str:
    if scan["error"] is not None:
        return str(scan["error"])
    tail_suffix = " / tail_corrupted_ignored" if scan["tail_corruption_ignored"] else ""
    if not scan["exists"]:
        return f"missing: {path}"
    return f"ok: {path} rows={scan['row_count']}{tail_suffix}"


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None, bool]:
    if not path.exists():
        return None, None, False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"{path} の読み込みに失敗: {exc}", True
    except json.JSONDecodeError as exc:
        return None, f"{path} の JSON が不正: {exc}", True
    if not isinstance(raw, dict):
        return None, f"{path} の JSON root は object である必要があります", True
    return raw, None, True


def _scan_events_jsonl(path: Path) -> dict[str, Any]:
    last_checkpoint: str | None = None
    last_incident_event: dict[str, Any] | None = None

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        nonlocal last_checkpoint, last_incident_event
        event_name = payload.get("event")
        if event_name == "checkpoint_advanced":
            checkpoint_value = payload.get("checkpoint")
            last_checkpoint = (
                checkpoint_value if isinstance(checkpoint_value, str) else None
            )
        if event_name in _INCIDENT_EVENT_TO_STATUS:
            kind_value = payload.get("kind")
            at_value = payload.get("at")
            last_incident_event = {
                "event": event_name,
                "status": _INCIDENT_EVENT_TO_STATUS[event_name],
                "kind": kind_value if isinstance(kind_value, str) else None,
                "at": at_value if isinstance(at_value, str) else None,
            }

    scan = _scan_jsonl(path, on_payload=on_payload)
    scan["last_checkpoint"] = last_checkpoint
    scan["last_incident_event"] = last_incident_event
    return scan


def _scan_runs_jsonl(path: Path) -> dict[str, Any]:
    last_run_payload: dict[str, Any] | None = None
    last_success_at: str | None = None

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        nonlocal last_run_payload, last_success_at
        last_run_payload = payload
        if payload.get("failure_kind") in (None, ""):
            ended_at_value = payload.get("ended_at")
            if isinstance(ended_at_value, str) and ended_at_value:
                last_success_at = ended_at_value

    scan = _scan_jsonl(path, on_payload=on_payload)
    summary = None
    if last_run_payload is not None:
        summary = {
            "last_run_status": "ok"
            if last_run_payload.get("failure_kind") in (None, "")
            else "error",
            "last_failure_kind": last_run_payload.get("failure_kind"),
            "checkpoint_before": last_run_payload.get("checkpoint_before"),
            "checkpoint_after": last_run_payload.get("checkpoint_after"),
            "auth_status": last_run_payload.get("auth_status"),
            "last_success_at": last_success_at,
        }

    scan["summary"] = summary
    return scan


def _scan_jsonl(path: Path, *, on_payload) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.exists(),
        "row_count": 0,
        "tail_corruption_ignored": False,
        "error": None,
    }
    if not result["exists"]:
        return result

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        result["error"] = f"{path} の stat 取得に失敗: {exc}"
        return result

    try:
        with path.open("rb") as handle:
            line_no = 0
            while True:
                line_start = handle.tell()
                raw_line = handle.readline()
                if raw_line == b"":
                    break
                line_no += 1
                line_end = handle.tell()
                is_tail_line = line_end == file_size
                stripped_line = raw_line.rstrip(b"\r\n")
                if not stripped_line:
                    continue

                try:
                    line = stripped_line.decode("utf-8")
                except UnicodeDecodeError:
                    if is_tail_line:
                        result["tail_corruption_ignored"] = True
                        continue
                    result["error"] = (
                        f"JSONL の途中行が破損しています: path={path} line={line_no}"
                    )
                    return result

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    if is_tail_line:
                        result["tail_corruption_ignored"] = True
                        continue
                    result["error"] = (
                        f"JSONL の途中行が破損しています: path={path} line={line_no}"
                    )
                    return result

                if isinstance(payload, dict):
                    result["row_count"] += 1
                    on_payload(line_start, payload)
    except OSError as exc:
        result["error"] = f"{path} の読み込みに失敗: {exc}"

    return result


def _state_last_message_id(state_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(state_payload, dict):
        return None
    value = state_payload.get("last_message_id")
    return value if isinstance(value, str) else None


def _active_incident_from_state(
    state_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(state_payload, dict):
        return None
    kind = state_payload.get("active_incident_kind")
    if not isinstance(kind, str) or not kind:
        return None

    message = state_payload.get("active_incident_message")
    at = state_payload.get("active_incident_at")
    suppressed_count = state_payload.get("incident_suppressed_count", 0)
    return {
        "kind": kind,
        "message": message if isinstance(message, str) else None,
        "at": at if isinstance(at, str) else None,
        "suppressed_count": _coerce_non_negative_int(suppressed_count),
    }


def _coerce_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return 0
    return converted if converted >= 0 else 0


def _derive_incident_status(
    *,
    state_payload: dict[str, Any] | None,
    latest_incident_event: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    active_incident = _active_incident_from_state(state_payload)
    if active_incident is not None:
        if active_incident.get("suppressed_count", 0) > 0:
            return active_incident, "suppressed"
        return active_incident, "open"

    if latest_incident_event is not None:
        status = latest_incident_event.get("status")
        if status == "recovered":
            return {
                "kind": latest_incident_event.get("kind"),
                "at": latest_incident_event.get("at"),
                "suppressed_count": 0,
            }, "recovered"

    return {}, "none"


def _normalize_summary(summary: object) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    if any(key not in summary for key in _SUMMARY_KEYS):
        return None
    return {key: summary.get(key) for key in _SUMMARY_KEYS}


def _check_checkpoint_consistency(
    *,
    events_frontier: str | None,
    state_checkpoint: str | None,
    events_scan_error: str | None,
) -> tuple[bool, str]:
    if events_scan_error is not None:
        return False, events_scan_error
    if events_frontier is None and state_checkpoint is None:
        return True, "frontier 未初期化"
    if events_frontier is None and state_checkpoint is not None:
        return True, "events frontier 不在のため state snapshot を使用"
    if state_checkpoint == events_frontier:
        return True, "state checkpoint は events frontier と一致"
    return (
        False,
        "state checkpoint が events frontier と不一致"
        f" (state={state_checkpoint}, events={events_frontier})",
    )


def _check_checkpoint_index_consistency(
    *,
    events_file: Path,
    events_frontier: str | None,
    checkpoint_index_payload: dict[str, Any] | None,
    checkpoint_index_error: str | None,
    checkpoint_index_exists: bool,
    events_scan_error: str | None,
) -> tuple[bool, str]:
    if checkpoint_index_error is not None:
        return False, checkpoint_index_error
    if not checkpoint_index_exists:
        return True, "checkpoint index 不在 (rebuild で再生成可)"
    if checkpoint_index_payload is None:
        return False, "checkpoint index の JSON が不正"
    if events_scan_error is not None:
        return False, events_scan_error

    offset = checkpoint_index_payload.get("offset")
    eof_size = checkpoint_index_payload.get("eof_size")
    index_checkpoint = checkpoint_index_payload.get("checkpoint")
    if not isinstance(offset, int) or offset < 0:
        return False, "checkpoint index offset が不正"
    if not isinstance(eof_size, int) or eof_size < 0:
        return False, "checkpoint index eof_size が不正"

    current_size = _safe_file_size(events_file)
    if current_size is None:
        return False, f"events file の stat に失敗: {events_file}"
    if eof_size > current_size:
        return False, "checkpoint index eof_size が events file size を超過"

    row = _read_jsonl_row_at_offset(events_file, offset)
    if row is None:
        return False, "checkpoint index offset の参照行を読み取れません"
    if row.get("event") != "checkpoint_advanced":
        return False, "checkpoint index offset が checkpoint_advanced を指していません"
    if row.get("checkpoint") != index_checkpoint:
        return False, "checkpoint index の checkpoint が参照行と不一致"
    if index_checkpoint != events_frontier:
        return (
            False,
            "checkpoint index checkpoint が events frontier と不一致"
            f" (index={index_checkpoint}, events={events_frontier})",
        )

    return True, "checkpoint index は events frontier と一致"


def _check_run_summary_consistency(
    *,
    runs_summary: dict[str, Any] | None,
    state_summary: dict[str, Any] | None,
    runs_scan_error: str | None,
) -> tuple[bool, str]:
    if runs_scan_error is not None:
        return False, runs_scan_error
    if runs_summary is None and state_summary is None:
        return True, "run summary 未初期化"
    if runs_summary is None and state_summary is not None:
        return False, "runs summary がないのに state last_run_summary が存在"
    if runs_summary is not None and state_summary is None:
        return False, "runs summary はあるが state last_run_summary が存在しない"
    if runs_summary == state_summary:
        return True, "state last_run_summary は runs summary と一致"
    return False, "state last_run_summary が runs summary と不一致"


def _check_run_summary_index_consistency(
    *,
    runs_file: Path,
    runs_summary: dict[str, Any] | None,
    runs_index_payload: dict[str, Any] | None,
    runs_index_summary: dict[str, Any] | None,
    runs_index_error: str | None,
    runs_index_exists: bool,
    runs_scan_error: str | None,
) -> tuple[bool, str]:
    if runs_index_error is not None:
        return False, runs_index_error
    if not runs_index_exists:
        return True, "runs summary index 不在 (rebuild で再生成可)"
    if runs_index_payload is None:
        return False, "runs summary index の JSON が不正"
    if runs_scan_error is not None:
        return False, runs_scan_error
    if runs_index_summary is None:
        return False, "runs summary index summary が不正"

    offset = runs_index_payload.get("offset")
    eof_size = runs_index_payload.get("eof_size")
    run_id = runs_index_payload.get("run_id")
    if not isinstance(offset, int) or offset < 0:
        return False, "runs summary index offset が不正"
    if not isinstance(eof_size, int) or eof_size < 0:
        return False, "runs summary index eof_size が不正"
    if not isinstance(run_id, str) or not run_id:
        return False, "runs summary index run_id が不正"

    current_size = _safe_file_size(runs_file)
    if current_size is None:
        return False, f"runs file の stat に失敗: {runs_file}"
    if eof_size > current_size:
        return False, "runs summary index eof_size が runs file size を超過"

    row = _read_jsonl_row_at_offset(runs_file, offset)
    if row is None:
        return False, "runs summary index offset の参照行を読み取れません"
    if row.get("run_id") != run_id:
        return False, "runs summary index run_id が参照行と不一致"

    if runs_summary is None:
        return False, "runs summary がないのに runs summary index が存在"
    if runs_index_summary != runs_summary:
        return False, "runs summary index summary が runs summary と不一致"

    return True, "runs summary index は runs summary と一致"


def _check_incident_consistency(
    *,
    state_payload: dict[str, Any] | None,
    latest_incident_event: dict[str, Any] | None,
    events_scan_error: str | None,
    state_error: str | None,
) -> tuple[bool, str]:
    if state_error is not None:
        return False, state_error
    if events_scan_error is not None:
        return False, events_scan_error

    active_incident = _active_incident_from_state(state_payload)
    if active_incident is None:
        if latest_incident_event is None:
            return True, "incident event なし / active incident なし"
        latest_status = latest_incident_event.get("status")
        if latest_status == "recovered":
            return True, "latest incident は recovered / active incident なし"
        return False, "latest incident が open/suppressed なのに active incident がない"

    if latest_incident_event is None:
        return True, "active incident あり / latest incident event なし"

    latest_status = latest_incident_event.get("status")
    if latest_status == "recovered":
        return False, "latest incident が recovered なのに active incident が残存"

    latest_kind = latest_incident_event.get("kind")
    active_kind = active_incident.get("kind")
    if isinstance(latest_kind, str) and latest_kind and latest_kind != active_kind:
        return False, "active incident kind と latest incident kind が不一致"

    return True, "active incident は latest incident と整合"


def _checkpoint_value_from_index(
    checkpoint_index_payload: dict[str, Any] | None,
) -> str | None:
    if not isinstance(checkpoint_index_payload, dict):
        return None
    value = checkpoint_index_payload.get("checkpoint")
    return value if isinstance(value, str) else None


def _read_jsonl_row_at_offset(path: Path, offset: int) -> dict[str, Any] | None:
    if not path.exists():
        return None

    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw_line = handle.readline()
    except OSError:
        return None

    if not raw_line:
        return None

    line = raw_line.rstrip(b"\r\n")
    if not line:
        return None

    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def _safe_file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def build_metrics_report(
    runtime: RuntimeConfig,
    *,
    recent_run_window: int = 50,
) -> dict[str, Any]:
    """Thin operational metrics for external monitors (JSON-friendly)."""
    now = datetime.now(UTC)
    events_scan = _scan_events_jsonl(runtime.events_file)
    runs_payloads = _load_runs_payloads(runtime.runs_file)

    last_cp_at = _last_checkpoint_advanced_at(runtime.events_file)
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

    incident_suppressed_total = _count_events_named(
        runtime.events_file, "incident_suppressed"
    )

    dedupe_info = _dedupe_metrics(runtime.discord_dedupe_state_file)
    state_payload, _state_err, _state_exists = _read_json_object(runtime.state_file)

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
        "incident": _metrics_incident_open_duration(state_payload, now),
        "last_run": {
            "run_id": last_run.get("run_id") if last_run else None,
            "ended_at": last_run.get("ended_at") if last_run else None,
            "failure_kind": last_run.get("failure_kind") if last_run else None,
        },
    }


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


def _load_runs_payloads(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        rows.append(payload)

    scan = _scan_jsonl(path, on_payload=on_payload)
    if scan.get("error"):
        return []
    return rows


def _last_checkpoint_advanced_at(path: Path) -> datetime | None:
    last_at: datetime | None = None

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        nonlocal last_at
        if payload.get("event") != "checkpoint_advanced":
            return
        at_val = payload.get("at")
        if not isinstance(at_val, str):
            return
        parsed = parse_utc_iso(at_val)
        if parsed is not None:
            last_at = parsed

    scan = _scan_jsonl(path, on_payload=on_payload)
    if scan.get("error"):
        return None
    return last_at


def _count_events_named(path: Path, event_name: str) -> int:
    count = 0

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        nonlocal count
        if payload.get("event") == event_name:
            count += 1

    scan = _scan_jsonl(path, on_payload=on_payload)
    if scan.get("error"):
        return 0
    return count


def _dedupe_metrics(path: Path) -> dict[str, Any]:
    payload, err, exists = _read_json_object(path)
    if err is not None or not exists:
        return {
            "state_file": str(path),
            "readable": False,
            "entry_count": 0,
        }
    if not isinstance(payload, dict):
        return {
            "state_file": str(path),
            "readable": False,
            "entry_count": 0,
        }
    entries = payload.get("entries", {})
    n = len(entries) if isinstance(entries, dict) else 0
    return {
        "state_file": str(path),
        "readable": True,
        "entry_count": n,
    }


def _metrics_incident_open_duration(
    state_payload: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    active = _active_incident_from_state(state_payload)
    if active is None:
        return {
            "status": "none",
            "kind": None,
            "open_duration_seconds": None,
            "suppressed_count": 0,
        }
    at = active.get("at")
    opened = parse_utc_iso(at) if isinstance(at, str) else None
    duration: float | None = None
    if opened is not None:
        duration = max(0.0, (now - opened).total_seconds())
    status = "suppressed" if int(active.get("suppressed_count") or 0) > 0 else "open"
    return {
        "status": status,
        "kind": active.get("kind"),
        "open_duration_seconds": duration,
        "suppressed_count": int(active.get("suppressed_count") or 0),
    }
