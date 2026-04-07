from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import load_state, save_state


class IncidentStateStore:
    def __init__(
        self,
        *,
        state_file: Path,
        append_event_fn: Callable[[str, str, dict[str, Any]], None],
    ):
        self.state_file = state_file
        self._append_event = append_event_fn

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
        self._append_event(
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
        self._append_event(
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

        self._append_event(
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
