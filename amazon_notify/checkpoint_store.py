from __future__ import annotations

import json
import os
from errno import ENOSPC
from pathlib import Path
from typing import Any

from .config import LOGGER, load_state, save_state
from .domain import Checkpoint, RunResult
from .errors import CheckpointError
from .gmail_transient_state import state_update_lock
from .incident_store import IncidentStateStore
from .time_utils import utc_now_iso

SCHEMA_VERSION = 1
MIGRATION_RUN_ID = "migration-bootstrap"

_CHECKPOINT_INDEX_SUFFIX = ".checkpoint.index.json"
_RUN_SUMMARY_INDEX_SUFFIX = ".summary.index.json"
_RUN_SUMMARY_STATE_KEY = "last_run_summary"


class _CheckpointIndex:
    def __init__(self, *, events_file: Path):
        self.path = events_file.with_name(f"{events_file.name}{_CHECKPOINT_INDEX_SUFFIX}")

    def read_payload(self, *, read_json_file) -> dict[str, Any] | None:
        return read_json_file(self.path)

    def update(self, *, checkpoint: object, offset: int, eof_size: int) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": checkpoint if isinstance(checkpoint, str) else None,
            "offset": offset,
            "eof_size": eof_size,
            "updated_at": utc_now_iso(),
        }
        try:
            save_state(self.path, payload)
        except OSError as exc:
            LOGGER.warning(
                "CHECKPOINT_INDEX_UPDATE_FAILED: path=%s error=%s",
                self.path,
                exc,
            )

    def remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("INDEX_FILE_REMOVE_FAILED: path=%s error=%s", self.path, exc)


class _RunSummaryIndex:
    def __init__(self, *, runs_file: Path):
        self.path = runs_file.with_name(f"{runs_file.name}{_RUN_SUMMARY_INDEX_SUFFIX}")

    def read_payload(self, *, read_json_file) -> dict[str, Any] | None:
        return read_json_file(self.path)

    def update(
        self,
        *,
        summary: dict[str, Any],
        run_id: str | None,
        offset: int,
        eof_size: int,
    ) -> None:
        if not run_id:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "offset": offset,
            "eof_size": eof_size,
            "summary": summary,
            "updated_at": utc_now_iso(),
        }
        try:
            save_state(self.path, payload)
        except OSError as exc:
            LOGGER.warning(
                "RUN_SUMMARY_INDEX_UPDATE_FAILED: path=%s error=%s",
                self.path,
                exc,
            )

    def remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("INDEX_FILE_REMOVE_FAILED: path=%s error=%s", self.path, exc)


def _is_disk_full_error(exc: OSError) -> bool:
    errno_value = getattr(exc, "errno", None)
    if errno_value == ENOSPC:
        return True
    return "no space left on device" in str(exc).lower()


def _format_storage_write_error(context: str, path: Path, exc: OSError) -> str:
    message = f"{context}に失敗しました: {exc} (path={path})"
    if _is_disk_full_error(exc):
        message += " / ディスク容量不足(ENOSPC)の可能性があります。"
    return message


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
        self._checkpoint_index = _CheckpointIndex(events_file=self.events_file)
        self._run_summary_index = _RunSummaryIndex(runs_file=self.runs_file)
        self.events_checkpoint_index_file = self._checkpoint_index.path
        self.runs_summary_index_file = self._run_summary_index.path
        self._incident_store = IncidentStateStore(
            state_file=self.state_file,
            event_appender=self,
        )

    def load_checkpoint(self) -> Checkpoint:
        checkpoint_from_index = self._load_checkpoint_from_index()
        if checkpoint_from_index is not None:
            return Checkpoint(message_id=checkpoint_from_index)

        # checkpoint の正本は events.jsonl とする。
        event_entries, eof_size = self._load_jsonl_entries(self.events_file)
        checkpoint_entry = self._find_last_checkpoint_entry(event_entries)
        if checkpoint_entry is not None:
            offset, event_payload = checkpoint_entry
            checkpoint = event_payload.get("checkpoint")
            self._update_checkpoint_index(
                checkpoint=checkpoint,
                offset=offset,
                eof_size=eof_size,
            )
            return Checkpoint(message_id=checkpoint)

        # 初回移行: events が空で state の checkpoint があれば 1 回だけ bootstrap する。
        state = load_state(self.state_file)
        bootstrap_checkpoint = state.get("last_message_id")
        if bootstrap_checkpoint and not event_entries:
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
                _format_storage_write_error(
                    "checkpoint event 保存",
                    self.events_file,
                    exc,
                ),
                checkpoint.message_id,
            ) from exc

        try:
            # 互換 snapshot: state.json は派生物としてベストエフォートで更新する。
            with state_update_lock(self.state_file):
                state = load_state(self.state_file)
                state["last_message_id"] = checkpoint.message_id
                save_state(self.state_file, state)
        except OSError as exc:
            LOGGER.warning(
                "STATE_SNAPSHOT_UPDATE_FAILED: checkpoint=%s error=%s",
                checkpoint.message_id,
                exc,
            )

    def append_event(
        self, event_type: str, run_id: str, payload: dict[str, Any]
    ) -> None:
        event = {
            "schema_version": SCHEMA_VERSION,
            "event": event_type,
            "run_id": run_id,
            "at": utc_now_iso(),
            **payload,
        }
        offset, eof_size = self._append_jsonl(self.events_file, event)
        if event_type != "checkpoint_advanced":
            return

        checkpoint = event.get("checkpoint")
        self._update_checkpoint_index(
            checkpoint=checkpoint,
            offset=offset,
            eof_size=eof_size,
        )

    def append_run_result(self, result: RunResult) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            **result.to_json_dict(),
        }
        try:
            offset, eof_size = self._append_jsonl(self.runs_file, payload)
        except OSError as exc:
            raise CheckpointError(
                _format_storage_write_error(
                    "run result 保存",
                    self.runs_file,
                    exc,
                ),
                result.checkpoint_after,
            ) from exc

        # 集計 cache 更新はベストエフォート。
        try:
            self._update_run_summary_caches(
                result=result, offset=offset, eof_size=eof_size
            )
        except Exception as exc:
            LOGGER.warning(
                "RUN_SUMMARY_CACHE_UPDATE_FAILED: run_id=%s error=%s",
                result.run_id,
                exc,
            )

    def load_last_run_summary(self) -> dict[str, Any] | None:
        summary_from_index = self._load_run_summary_from_index()
        if summary_from_index is not None:
            return summary_from_index

        summary_from_state = self._load_run_summary_from_state()
        if summary_from_state is not None:
            return summary_from_state

        run_entries, eof_size = self._load_jsonl_entries(self.runs_file)
        if not run_entries:
            return None

        summary = self._build_summary_from_runs([row for _, row in run_entries])
        last_offset, last_payload = run_entries[-1]
        run_id = last_payload.get("run_id")
        self._update_run_summary_index(
            summary=summary,
            run_id=run_id if isinstance(run_id, str) else None,
            offset=last_offset,
            eof_size=eof_size,
        )
        self._update_state_summary_snapshot(summary)
        return summary

    def load_incident_state(self) -> dict[str, Any] | None:
        return self._incident_store.load_incident_state()

    def suppress_incident(self, *, kind: str, run_id: str) -> int:
        return self._incident_store.suppress_incident(kind=kind, run_id=run_id)

    def open_incident(
        self,
        *,
        kind: str,
        message: str | None,
        opened_at: str,
        run_id: str,
    ) -> None:
        self._incident_store.open_incident(
            kind=kind,
            message=message,
            opened_at=opened_at,
            run_id=run_id,
        )

    def recover_incident(self, *, run_id: str) -> dict[str, Any] | None:
        return self._incident_store.recover_incident(run_id=run_id)

    def rebuild_indexes(self) -> dict[str, bool]:
        rebuilt = {
            "checkpoint_index": False,
            "run_summary_index": False,
        }

        event_entries, events_eof_size = self._load_jsonl_entries(self.events_file)
        checkpoint_entry = self._find_last_checkpoint_entry(event_entries)
        if checkpoint_entry is not None:
            checkpoint_offset, checkpoint_payload = checkpoint_entry
            self._update_checkpoint_index(
                checkpoint=checkpoint_payload.get("checkpoint"),
                offset=checkpoint_offset,
                eof_size=events_eof_size,
            )
            rebuilt["checkpoint_index"] = True
        else:
            self._checkpoint_index.remove()

        run_entries, runs_eof_size = self._load_jsonl_entries(self.runs_file)
        if run_entries:
            summary = self._build_summary_from_runs([row for _, row in run_entries])
            run_offset, run_payload = run_entries[-1]
            run_id_obj = run_payload.get("run_id")
            run_id = run_id_obj if isinstance(run_id_obj, str) else None
            self._update_run_summary_index(
                summary=summary,
                run_id=run_id,
                offset=run_offset,
                eof_size=runs_eof_size,
            )
            self._update_state_summary_snapshot(summary)
            rebuilt["run_summary_index"] = True
        else:
            self._run_summary_index.remove()

        return rebuilt

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> tuple[int, int]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            with path.open("ab") as handle:
                offset = handle.tell()
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                eof_size = handle.tell()
            return offset, eof_size
        except OSError as exc:
            LOGGER.error("JSONL_WRITE_FAILED: path=%s error=%s", path, exc)
            raise

    def _load_jsonl_records(self, path: Path) -> list[dict[str, Any]]:
        entries, _ = self._load_jsonl_entries(path)
        return [payload for _, payload in entries]

    def _load_jsonl_entries(
        self,
        path: Path,
        *,
        start_offset: int = 0,
    ) -> tuple[list[tuple[int, dict[str, Any]]], int]:
        if not path.exists():
            return [], 0

        file_size = path.stat().st_size
        if start_offset < 0 or start_offset > file_size:
            raise CheckpointError(
                f"JSONL offset が不正です: path={path} offset={start_offset} size={file_size}"
            )

        entries: list[tuple[int, dict[str, Any]]] = []
        with path.open("rb") as handle:
            handle.seek(start_offset)
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
                try:
                    line = stripped_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    if is_tail_line:
                        LOGGER.warning(
                            "JSONL_TAIL_CORRUPTED_IGNORED: %s line=%s", path, line_no
                        )
                        continue
                    raise CheckpointError(
                        f"JSONL の途中行が破損しています: path={path} line={line_no}"
                    ) from exc

                if not line.strip():
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    # 末尾 1 行破損は復旧可能な前提で無視する。
                    if is_tail_line:
                        LOGGER.warning(
                            "JSONL_TAIL_CORRUPTED_IGNORED: %s line=%s", path, line_no
                        )
                        continue
                    raise CheckpointError(
                        f"JSONL の途中行が破損しています: path={path} line={line_no}"
                    ) from exc

                if isinstance(payload, dict):
                    entries.append((line_start, payload))

        return entries, file_size

    def _load_checkpoint_from_index(self) -> str | None:
        payload = self._checkpoint_index.read_payload(read_json_file=self._read_json_file)
        if payload is None:
            return None

        validated = self._validate_checkpoint_index(payload)
        if validated is None:
            return None
        checkpoint, offset, eof_size = validated

        current_size = self._safe_file_size(self.events_file)
        if current_size is None:
            return None

        if current_size < eof_size:
            return None

        if current_size > eof_size:
            latest = self._scan_checkpoint_incremental(
                checkpoint=checkpoint,
                offset=offset,
                eof_size=eof_size,
            )
            if latest is None:
                return None
            checkpoint = latest

        return checkpoint if isinstance(checkpoint, str) else None

    def _validate_checkpoint_index(
        self,
        payload: dict[str, Any],
    ) -> tuple[object, int, int] | None:
        checkpoint = payload.get("checkpoint")
        offset = payload.get("offset")
        eof_size = payload.get("eof_size")
        if not isinstance(offset, int) or offset < 0:
            return None
        if not isinstance(eof_size, int) or eof_size < 0:
            return None

        row = self._read_jsonl_row_at_offset(self.events_file, offset)
        if row is None:
            return None
        if row.get("event") != "checkpoint_advanced":
            return None
        if row.get("checkpoint") != checkpoint:
            return None

        return checkpoint, offset, eof_size

    def _scan_checkpoint_incremental(
        self,
        *,
        checkpoint: object,
        offset: int,
        eof_size: int,
    ) -> object | None:
        appended_entries, updated_size = self._load_jsonl_entries(
            self.events_file,
            start_offset=eof_size,
        )
        last_checkpoint_entry = self._find_last_checkpoint_entry(appended_entries)
        if last_checkpoint_entry is not None:
            last_offset, last_payload = last_checkpoint_entry
            latest_checkpoint = last_payload.get("checkpoint")
            self._update_checkpoint_index(
                checkpoint=latest_checkpoint,
                offset=last_offset,
                eof_size=updated_size,
            )
            return latest_checkpoint

        self._update_checkpoint_index(
            checkpoint=checkpoint,
            offset=offset,
            eof_size=updated_size,
        )
        return checkpoint

    def _update_checkpoint_index(
        self,
        *,
        checkpoint: object,
        offset: int,
        eof_size: int,
    ) -> None:
        self._checkpoint_index.update(
            checkpoint=checkpoint,
            offset=offset,
            eof_size=eof_size,
        )

    def _load_run_summary_from_index(self) -> dict[str, Any] | None:
        payload = self._run_summary_index.read_payload(read_json_file=self._read_json_file)
        if payload is None:
            return None

        summary = self._normalize_summary(payload.get("summary"))
        run_id = payload.get("run_id")
        offset = payload.get("offset")
        eof_size = payload.get("eof_size")
        if summary is None:
            return None
        if not isinstance(run_id, str) or not run_id:
            return None
        if not isinstance(offset, int) or offset < 0:
            return None
        if not isinstance(eof_size, int) or eof_size < 0:
            return None

        row = self._read_jsonl_row_at_offset(self.runs_file, offset)
        if row is None or row.get("run_id") != run_id:
            return None

        current_size = self._safe_file_size(self.runs_file)
        if current_size is None:
            return None

        if current_size < eof_size:
            return None

        latest_summary = summary
        latest_run_id = run_id
        latest_offset = offset
        latest_eof_size = eof_size

        if current_size > eof_size:
            appended_entries, latest_eof_size = self._load_jsonl_entries(
                self.runs_file,
                start_offset=eof_size,
            )
            for entry_offset, run_payload in appended_entries:
                latest_summary = self._summary_from_run_payload(
                    latest_summary, run_payload
                )
                candidate_run_id = run_payload.get("run_id")
                if isinstance(candidate_run_id, str) and candidate_run_id:
                    latest_run_id = candidate_run_id
                latest_offset = entry_offset

        if (
            latest_run_id != run_id
            or latest_offset != offset
            or latest_eof_size != eof_size
            or latest_summary != summary
        ):
            self._update_run_summary_index(
                summary=latest_summary,
                run_id=latest_run_id,
                offset=latest_offset,
                eof_size=latest_eof_size,
            )

        self._update_state_summary_snapshot(latest_summary)
        return latest_summary

    def _update_run_summary_caches(
        self, *, result: RunResult, offset: int, eof_size: int
    ) -> None:
        previous_summary = (
            self._read_summary_from_index_snapshot()
            or self._load_run_summary_from_state()
        )
        summary = self._summary_from_result(result, previous_summary)
        self._update_run_summary_index(
            summary=summary,
            run_id=result.run_id,
            offset=offset,
            eof_size=eof_size,
        )
        self._update_state_summary_snapshot(summary)

    def _update_run_summary_index(
        self,
        *,
        summary: dict[str, Any],
        run_id: str | None,
        offset: int,
        eof_size: int,
    ) -> None:
        self._run_summary_index.update(
            summary=summary,
            run_id=run_id,
            offset=offset,
            eof_size=eof_size,
        )

    def _update_state_summary_snapshot(self, summary: dict[str, Any]) -> None:
        try:
            with state_update_lock(self.state_file):
                state = load_state(self.state_file)
                state[_RUN_SUMMARY_STATE_KEY] = summary
                save_state(self.state_file, state)
        except Exception as exc:
            LOGGER.warning(
                "STATE_SUMMARY_SNAPSHOT_UPDATE_FAILED: path=%s error=%s",
                self.state_file,
                exc,
            )

    def _read_summary_from_index_snapshot(self) -> dict[str, Any] | None:
        payload = self._run_summary_index.read_payload(read_json_file=self._read_json_file)
        if payload is None:
            return None
        return self._normalize_summary(payload.get("summary"))

    def _load_run_summary_from_state(self) -> dict[str, Any] | None:
        try:
            state = load_state(self.state_file)
        except Exception:
            return None
        return self._normalize_summary(state.get(_RUN_SUMMARY_STATE_KEY))

    def _normalize_summary(self, summary: object) -> dict[str, Any] | None:
        if not isinstance(summary, dict):
            return None

        keys = (
            "last_run_status",
            "last_failure_kind",
            "checkpoint_before",
            "checkpoint_after",
            "auth_status",
            "last_success_at",
        )
        if any(key not in summary for key in keys):
            return None

        return {key: summary.get(key) for key in keys}

    def _summary_from_result(
        self,
        result: RunResult,
        previous_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        is_success = result.failure_kind is None
        last_success_at = (
            result.ended_at
            if is_success
            else (previous_summary or {}).get("last_success_at")
        )
        return {
            "last_run_status": "ok" if is_success else "error",
            "last_failure_kind": result.failure_kind.value
            if result.failure_kind
            else None,
            "checkpoint_before": result.checkpoint_before,
            "checkpoint_after": result.checkpoint_after,
            "auth_status": result.auth_status.value if result.auth_status else None,
            "last_success_at": last_success_at,
        }

    def _summary_from_run_payload(
        self,
        current_summary: dict[str, Any],
        run_payload: dict[str, Any],
    ) -> dict[str, Any]:
        is_success = run_payload.get("failure_kind") in (None, "")
        last_success_at = (
            run_payload.get("ended_at")
            if is_success
            else current_summary.get("last_success_at")
        )
        return {
            "last_run_status": "ok" if is_success else "error",
            "last_failure_kind": run_payload.get("failure_kind"),
            "checkpoint_before": run_payload.get("checkpoint_before"),
            "checkpoint_after": run_payload.get("checkpoint_after"),
            "auth_status": run_payload.get("auth_status"),
            "last_success_at": last_success_at,
        }

    def _build_summary_from_runs(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        last = runs[-1]
        last_success = next(
            (item for item in reversed(runs) if item.get("failure_kind") in (None, "")),
            None,
        )
        return {
            "last_run_status": "ok"
            if last.get("failure_kind") in (None, "")
            else "error",
            "last_failure_kind": last.get("failure_kind"),
            "checkpoint_before": last.get("checkpoint_before"),
            "checkpoint_after": last.get("checkpoint_after"),
            "auth_status": last.get("auth_status"),
            "last_success_at": last_success.get("ended_at") if last_success else None,
        }

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _read_jsonl_row_at_offset(
        self, path: Path, offset: int
    ) -> dict[str, Any] | None:
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

    def _safe_file_size(self, path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _find_last_checkpoint_entry(
        entries: list[tuple[int, dict[str, Any]]],
    ) -> tuple[int, dict[str, Any]] | None:
        for offset, event in reversed(entries):
            if event.get("event") == "checkpoint_advanced":
                return offset, event
        return None

    @staticmethod
    def _find_last_checkpoint_event(
        events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("event") == "checkpoint_advanced":
                return event
        return None
