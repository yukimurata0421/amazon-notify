from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import LOGGER, load_state, save_state
from .domain import Checkpoint, RunResult
from .errors import CheckpointError
from .time_utils import utc_now_iso

SCHEMA_VERSION = 1
MIGRATION_RUN_ID = "migration-bootstrap"


class JsonlCheckpointStore:
    def __init__(
        self,
        state_file: Path,
        events_file: Path | None = None,
        runs_file: Path | None = None,
    ):
        self.state_file = state_file
        self.events_file = events_file or state_file.with_name("events.jsonl")
        self.runs_file = runs_file or state_file.with_name("runs.jsonl")

    def load_checkpoint(self) -> Checkpoint:
        # v0.3.0 以降は events.jsonl を正本とする。
        events = self._load_jsonl_records(self.events_file)
        checkpoint_event = self._find_last_checkpoint_event(events)
        if checkpoint_event is not None:
            return Checkpoint(message_id=checkpoint_event.get("checkpoint"))

        # 初回移行: events が空で state の checkpoint があれば 1 回だけ bootstrap する。
        state = load_state(self.state_file)
        bootstrap_checkpoint = state.get("last_message_id")
        if bootstrap_checkpoint and not events:
            self.append_event(
                "checkpoint_advanced",
                MIGRATION_RUN_ID,
                {
                    "checkpoint": bootstrap_checkpoint,
                    "bootstrap": True,
                    "source": "state_snapshot",
                },
            )

        return Checkpoint(message_id=bootstrap_checkpoint)

    def advance_checkpoint(self, checkpoint: Checkpoint, run_id: str) -> None:
        try:
            self.append_event(
                "checkpoint_advanced",
                run_id,
                {
                    "checkpoint": checkpoint.message_id,
                    "source": "pipeline_commit",
                },
            )
        except OSError as exc:
            raise CheckpointError(
                f"checkpoint event 保存に失敗しました: {exc}",
                checkpoint.message_id,
            ) from exc

        try:
            # 互換 snapshot: state.json は派生物としてベストエフォートで更新する。
            state = load_state(self.state_file)
            state["last_message_id"] = checkpoint.message_id
            save_state(self.state_file, state)
        except OSError as exc:
            LOGGER.warning(
                "STATE_SNAPSHOT_UPDATE_FAILED: checkpoint=%s error=%s",
                checkpoint.message_id,
                exc,
            )

    def append_event(self, event_type: str, run_id: str, payload: dict[str, Any]) -> None:
        event = {
            "schema_version": SCHEMA_VERSION,
            "event": event_type,
            "run_id": run_id,
            "at": utc_now_iso(),
            **payload,
        }
        self._append_jsonl(self.events_file, event)

    def append_run_result(self, result: RunResult) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            **result.to_json_dict(),
        }
        self._append_jsonl(self.runs_file, payload)

    def load_last_run_summary(self) -> dict[str, Any] | None:
        runs = self._load_jsonl_records(self.runs_file)
        if not runs:
            return None

        last = runs[-1]
        last_success = next(
            (item for item in reversed(runs) if item.get("failure_kind") in (None, "")),
            None,
        )
        return {
            "last_run_status": "ok" if last.get("failure_kind") in (None, "") else "error",
            "last_failure_kind": last.get("failure_kind"),
            "checkpoint_before": last.get("checkpoint_before"),
            "checkpoint_after": last.get("checkpoint_after"),
            "auth_status": last.get("auth_status"),
            "last_success_at": last_success.get("ended_at") if last_success else None,
        }

    def load_incident_state(self) -> dict[str, Any] | None:
        state = load_state(self.state_file)
        if not state.get("active_incident_kind"):
            return None
        return {
            "kind": state.get("active_incident_kind"),
            "at": state.get("active_incident_at"),
            "suppressed_count": state.get("incident_suppressed_count", 0),
            "message": state.get("active_incident_message"),
        }

    def suppress_incident(self, *, kind: str, run_id: str) -> int:
        state = load_state(self.state_file)
        suppressed_count = int(state.get("incident_suppressed_count", 0)) + 1
        state["incident_suppressed_count"] = suppressed_count
        save_state(self.state_file, state)
        self.append_event(
            "incident_suppressed",
            run_id,
            {
                "kind": kind,
                "suppressed_count": suppressed_count,
            },
        )
        return suppressed_count

    def open_incident(
        self,
        *,
        kind: str,
        message: str | None,
        opened_at: str,
        run_id: str,
    ) -> None:
        state = load_state(self.state_file)
        state["active_incident_kind"] = kind
        state["active_incident_message"] = message
        state["active_incident_at"] = opened_at
        state["incident_suppressed_count"] = 0
        save_state(self.state_file, state)
        self.append_event(
            "incident_opened",
            run_id,
            {
                "kind": kind,
            },
        )

    def recover_incident(self, *, run_id: str) -> dict[str, Any] | None:
        state = load_state(self.state_file)
        kind = state.get("active_incident_kind")
        if not kind:
            return None

        message = state.get("active_incident_message")
        at = state.get("active_incident_at")
        suppressed_count = int(state.get("incident_suppressed_count", 0))

        self.append_event(
            "incident_recovered",
            run_id,
            {
                "kind": kind,
            },
        )
        state.pop("active_incident_kind", None)
        state.pop("active_incident_message", None)
        state.pop("active_incident_at", None)
        state.pop("incident_suppressed_count", None)
        save_state(self.state_file, state)
        return {
            "kind": kind,
            "message": message,
            "at": at,
            "suppressed_count": suppressed_count,
        }

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load_jsonl_records(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []

        lines = path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        total = len(lines)
        for idx, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # 末尾 1 行破損は復旧可能な前提で無視する。
                if idx == total - 1:
                    LOGGER.warning("JSONL_TAIL_CORRUPTED_IGNORED: %s line=%s", path, idx + 1)
                    continue
                raise CheckpointError(
                    f"JSONL の途中行が破損しています: path={path} line={idx + 1}"
                )
            if isinstance(payload, dict):
                records.append(payload)
        return records

    @staticmethod
    def _find_last_checkpoint_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("event") == "checkpoint_advanced":
                return event
        return None
