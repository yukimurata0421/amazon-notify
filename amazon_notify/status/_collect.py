from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..discord_client import load_dedupe_summary
from ..time_utils import parse_utc_iso

SUMMARY_KEYS = (
    "last_run_status",
    "last_failure_kind",
    "checkpoint_before",
    "checkpoint_after",
    "auth_status",
    "last_success_at",
)
INCIDENT_EVENT_TO_STATUS = {
    "incident_opened": "open",
    "incident_suppressed": "suppressed",
    "incident_recovered": "recovered",
}


def read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None, bool]:
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


def scan_events_jsonl(path: Path) -> dict[str, Any]:
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
        if event_name in INCIDENT_EVENT_TO_STATUS:
            kind_value = payload.get("kind")
            at_value = payload.get("at")
            last_incident_event = {
                "event": event_name,
                "status": INCIDENT_EVENT_TO_STATUS[event_name],
                "kind": kind_value if isinstance(kind_value, str) else None,
                "at": at_value if isinstance(at_value, str) else None,
            }

    scan = scan_jsonl(path, on_payload=on_payload)
    scan["last_checkpoint"] = last_checkpoint
    scan["last_incident_event"] = last_incident_event
    return scan


def scan_runs_jsonl(path: Path) -> dict[str, Any]:
    last_run_payload: dict[str, Any] | None = None
    last_success_at: str | None = None

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        nonlocal last_run_payload, last_success_at
        last_run_payload = payload
        if payload.get("failure_kind") in (None, ""):
            ended_at_value = payload.get("ended_at")
            if isinstance(ended_at_value, str) and ended_at_value:
                last_success_at = ended_at_value

    scan = scan_jsonl(path, on_payload=on_payload)
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


def scan_jsonl(path: Path, *, on_payload) -> dict[str, Any]:
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


def state_last_message_id(state_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(state_payload, dict):
        return None
    value = state_payload.get("last_message_id")
    return value if isinstance(value, str) else None


def active_incident_from_state(
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
        "suppressed_count": coerce_non_negative_int(suppressed_count),
    }


def coerce_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return 0
    return converted if converted >= 0 else 0


def derive_incident_status(
    *,
    state_payload: dict[str, Any] | None,
    latest_incident_event: dict[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    active_incident = active_incident_from_state(state_payload)
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


def normalize_summary(summary: object) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    if any(key not in summary for key in SUMMARY_KEYS):
        return None
    return {key: summary.get(key) for key in SUMMARY_KEYS}


def checkpoint_value_from_index(
    checkpoint_index_payload: dict[str, Any] | None,
) -> str | None:
    if not isinstance(checkpoint_index_payload, dict):
        return None
    value = checkpoint_index_payload.get("checkpoint")
    return value if isinstance(value, str) else None


def read_jsonl_row_at_offset(path: Path, offset: int) -> dict[str, Any] | None:
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


def safe_file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def load_runs_payloads(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        rows.append(payload)

    scan = scan_jsonl(path, on_payload=on_payload)
    if scan.get("error"):
        return []
    return rows


def last_checkpoint_advanced_at(path: Path) -> datetime | None:
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

    scan = scan_jsonl(path, on_payload=on_payload)
    if scan.get("error"):
        return None
    return last_at


def count_events_named(path: Path, event_name: str) -> int:
    count = 0

    def on_payload(_offset: int, payload: dict[str, Any]) -> None:
        nonlocal count
        if payload.get("event") == event_name:
            count += 1

    scan = scan_jsonl(path, on_payload=on_payload)
    if scan.get("error"):
        return 0
    return count


def dedupe_metrics(path: Path) -> dict[str, Any]:
    payload, err, exists = read_json_object(path)
    if err is not None or not exists:
        return {
            "state_file": str(path),
            "readable": False,
            "entry_count": 0,
            "dedupe_hit_count": 0,
        }
    if not isinstance(payload, dict):
        return {
            "state_file": str(path),
            "readable": False,
            "entry_count": 0,
            "dedupe_hit_count": 0,
        }
    try:
        summary = load_dedupe_summary(path)
    except Exception:
        summary = {
            "entry_count": 0,
            "dedupe_hit_count": 0,
            "hit_window_seconds": 0.0,
            "readable": False,
        }
    return {
        "state_file": str(path),
        "readable": bool(summary.get("readable", True)),
        "entry_count": int(summary.get("entry_count", 0)),
        "dedupe_hit_count": int(summary.get("dedupe_hit_count", 0)),
        "hit_window_seconds": float(summary.get("hit_window_seconds", 0.0)),
    }


def metrics_incident_open_duration(
    state_payload: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    active = active_incident_from_state(state_payload)
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
