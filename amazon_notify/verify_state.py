from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .runtime import RuntimeConfig
from .status import build_doctor_report

_INCIDENT_EVENTS = {"incident_opened", "incident_suppressed", "incident_recovered"}


def run_verify_state(runtime: RuntimeConfig) -> tuple[int, dict[str, Any]]:
    _base_exit_code, base_report = build_doctor_report(runtime)
    extra_checks = _build_extra_checks(runtime)

    checks = [*base_report.get("checks", []), *extra_checks]
    status_value = "ok" if all(bool(item.get("ok")) for item in checks) else "degraded"

    report = dict(base_report)
    report["status"] = status_value
    report["checks"] = checks
    report["verify_state"] = {
        "extra_check_count": len(extra_checks),
    }
    return (0 if status_value == "ok" else 1), report


def _build_extra_checks(runtime: RuntimeConfig) -> list[dict[str, str | bool]]:
    checks: list[dict[str, str | bool]] = []

    frontier_ok, frontier_detail = _check_checkpoint_timestamp_monotonic(runtime.events_file)
    checks.append(
        {
            "name": "checkpoint_event_timestamp_monotonic",
            "ok": frontier_ok,
            "detail": frontier_detail,
        }
    )

    lifecycle_ok, lifecycle_detail = _check_incident_event_lifecycle(runtime.events_file)
    checks.append(
        {
            "name": "incident_event_lifecycle_valid",
            "ok": lifecycle_ok,
            "detail": lifecycle_detail,
        }
    )

    return checks


def _check_checkpoint_timestamp_monotonic(events_file: Path) -> tuple[bool, str]:
    rows, error = _read_jsonl_rows(events_file)
    if error is not None:
        return False, error
    checkpoint_rows = [row for row in rows if row.get("event") == "checkpoint_advanced"]
    if len(checkpoint_rows) <= 1:
        return True, "checkpoint_advanced が 0/1 件のため単調性検査をスキップ"

    previous_at: datetime | None = None
    for idx, row in enumerate(checkpoint_rows, start=1):
        at_value = row.get("at")
        if not isinstance(at_value, str) or not at_value:
            return False, f"checkpoint_advanced#{idx} の at が不正"
        parsed = _parse_ts(at_value)
        if parsed is None:
            return False, f"checkpoint_advanced#{idx} の at を解析できません: {at_value}"
        if previous_at is not None and parsed < previous_at:
            return False, "checkpoint_advanced の at が逆行しています"
        previous_at = parsed

    return True, "checkpoint_advanced の at は単調増加(非減少)"


def _check_incident_event_lifecycle(events_file: Path) -> tuple[bool, str]:
    rows, error = _read_jsonl_rows(events_file)
    if error is not None:
        return False, error

    active_kind: str | None = None
    has_incident_event = False
    for row in rows:
        event = row.get("event")
        if event not in _INCIDENT_EVENTS:
            continue
        has_incident_event = True

        kind_obj = row.get("kind")
        kind = kind_obj if isinstance(kind_obj, str) and kind_obj else None

        if event == "incident_opened":
            if active_kind is not None:
                return False, "incident_opened が recovery 前に連続しています"
            active_kind = kind
            continue

        if event == "incident_suppressed":
            if active_kind is None:
                return False, "incident_suppressed が open 前に出現しています"
            if kind is not None and active_kind is not None and kind != active_kind:
                return False, "incident_suppressed の kind が active incident と不一致"
            continue

        if event == "incident_recovered":
            if active_kind is None:
                return False, "incident_recovered が open 前に出現しています"
            if kind is not None and active_kind is not None and kind != active_kind:
                return False, "incident_recovered の kind が active incident と不一致"
            active_kind = None

    if not has_incident_event:
        return True, "incident event が存在しないため lifecycle は妥当"

    return True, "incident event lifecycle は妥当"


def _read_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not path.exists():
        return [], None

    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], f"{path} の stat に失敗: {exc}"

    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            line_no = 0
            while True:
                raw = handle.readline()
                if raw == b"":
                    break
                line_no += 1
                is_tail = handle.tell() == size
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    decoded = line.decode("utf-8")
                    payload = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if is_tail:
                        continue
                    return [], f"JSONL の途中行が破損: path={path} line={line_no}"
                if isinstance(payload, dict):
                    rows.append(payload)
    except OSError as exc:
        return [], f"{path} の読み込みに失敗: {exc}"

    return rows, None


def _parse_ts(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
