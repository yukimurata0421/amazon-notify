from __future__ import annotations

import json

from ..checkpoint_store import JsonlCheckpointStore
from ..status import build_doctor_report
from . import register_scenario


@register_scenario("checkpoint_interrupt_window")
class CheckpointInterruptWindowScenario:
    name = "checkpoint_interrupt_window"

    def setup(self, runtime) -> None:
        runtime.state_file.write_text(json.dumps({"last_message_id": "cp-1"}), encoding="utf-8")
        runtime.events_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "checkpoint_advanced",
                    "run_id": "run-1",
                    "at": "2026-01-01T00:00:00+00:00",
                    "checkpoint": "cp-1",
                    "source": "pipeline_commit",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runtime.runs_file.write_text("", encoding="utf-8")

    def verify(self, runtime) -> dict:
        store = JsonlCheckpointStore(
            state_file=runtime.state_file,
            events_file=runtime.events_file,
            runs_file=runtime.runs_file,
        )
        rebuilt = store.rebuild_indexes()
        code, _report = build_doctor_report(runtime)
        return {
            "ok": rebuilt["checkpoint_index"] and code == 0,
            "checkpoint_index_rebuilt": rebuilt["checkpoint_index"],
        }
