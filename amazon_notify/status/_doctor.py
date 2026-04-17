from __future__ import annotations

from pathlib import Path
from typing import Any

from ..runtime import RuntimeConfig
from ..time_utils import utc_now_iso
from ._collect import (
    active_incident_from_state,
    checkpoint_value_from_index,
    derive_incident_status,
    normalize_summary,
    read_json_object,
    read_jsonl_row_at_offset,
    safe_file_size,
    scan_events_jsonl,
    scan_runs_jsonl,
    state_last_message_id,
)


def build_runtime_report(runtime: RuntimeConfig) -> dict[str, Any]:
    state_payload, state_error, state_exists = read_json_object(runtime.state_file)
    events_scan = scan_events_jsonl(runtime.events_file)
    runs_scan = scan_runs_jsonl(runtime.runs_file)

    checkpoint_index_payload, checkpoint_index_error, checkpoint_index_exists = (
        read_json_object(
            runtime.events_file.with_name(
                f"{runtime.events_file.name}.checkpoint.index.json"
            )
        )
    )
    runs_index_payload, runs_index_error, runs_index_exists = read_json_object(
        runtime.runs_file.with_name(f"{runtime.runs_file.name}.summary.index.json")
    )

    state_last_cp = state_last_message_id(state_payload)
    runs_state_summary = normalize_summary(
        state_payload.get("last_run_summary") if isinstance(state_payload, dict) else None
    )

    frontier = events_scan["last_checkpoint"]
    if frontier is None and state_last_cp is not None:
        frontier = state_last_cp

    runs_summary = runs_scan["summary"]
    last_success_at = runs_summary.get("last_success_at") if runs_summary else None
    last_failure_kind = runs_summary.get("last_failure_kind") if runs_summary else None

    incident, incident_status = derive_incident_status(
        state_payload=state_payload,
        latest_incident_event=events_scan["last_incident_event"],
    )

    checks: list[dict[str, str | bool]] = []
    checks.append(
        {
            "name": "state_json_readable",
            "ok": state_error is None,
            "detail": _readable_detail(runtime.state_file, state_exists, state_error),
        }
    )
    checks.append(
        {
            "name": "events_jsonl_readable",
            "ok": events_scan["error"] is None,
            "detail": _jsonl_detail(runtime.events_file, events_scan),
        }
    )
    checks.append(
        {
            "name": "runs_jsonl_readable",
            "ok": runs_scan["error"] is None,
            "detail": _jsonl_detail(runtime.runs_file, runs_scan),
        }
    )

    checkpoint_ok, checkpoint_detail = check_checkpoint_consistency(
        events_frontier=events_scan["last_checkpoint"],
        state_checkpoint=state_last_cp,
        events_scan_error=events_scan["error"],
    )
    checks.append(
        {
            "name": "checkpoint_state_consistent",
            "ok": checkpoint_ok,
            "detail": checkpoint_detail,
        }
    )

    checkpoint_index_ok, checkpoint_index_detail = check_checkpoint_index_consistency(
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

    runs_index_summary = normalize_summary(
        runs_index_payload.get("summary") if isinstance(runs_index_payload, dict) else None
    )
    run_summary_ok, run_summary_detail = check_run_summary_consistency(
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

    runs_index_ok, runs_index_detail = check_run_summary_index_consistency(
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

    incident_ok, incident_detail = check_incident_consistency(
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
            "checkpoint_consistent": checkpoint_ok and checkpoint_index_ok,
            "run_summary_consistent": run_summary_ok and runs_index_ok,
            "incident_consistent": incident_ok,
        },
        "runtime_status": {
            "events_frontier": events_scan["last_checkpoint"],
            "state_checkpoint": state_last_cp,
            "checkpoint_index": checkpoint_value_from_index(checkpoint_index_payload),
            "runs_summary_from_runs": runs_summary,
            "runs_summary_from_state": runs_state_summary,
            "runs_summary_from_index": runs_index_summary,
            "latest_incident_event": events_scan["last_incident_event"],
            "active_incident_state": active_incident_from_state(state_payload),
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


def check_checkpoint_consistency(
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


def check_checkpoint_index_consistency(
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

    current_size = safe_file_size(events_file)
    if current_size is None:
        return False, f"events file の stat に失敗: {events_file}"
    if eof_size > current_size:
        return False, "checkpoint index eof_size が events file size を超過"

    row = read_jsonl_row_at_offset(events_file, offset)
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


def check_run_summary_consistency(
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


def check_run_summary_index_consistency(
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

    current_size = safe_file_size(runs_file)
    if current_size is None:
        return False, f"runs file の stat に失敗: {runs_file}"
    if eof_size > current_size:
        return False, "runs summary index eof_size が runs file size を超過"

    row = read_jsonl_row_at_offset(runs_file, offset)
    if row is None:
        return False, "runs summary index offset の参照行を読み取れません"
    if row.get("run_id") != run_id:
        return False, "runs summary index run_id が参照行と不一致"

    if runs_summary is None:
        return False, "runs summary がないのに runs summary index が存在"
    if runs_index_summary != runs_summary:
        return False, "runs summary index summary が runs summary と不一致"

    return True, "runs summary index は runs summary と一致"


def check_incident_consistency(
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

    active_incident = active_incident_from_state(state_payload)
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
