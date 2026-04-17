from __future__ import annotations

import json

from ..status import build_doctor_report
from . import register_scenario


@register_scenario("corrupted_jsonl_middle")
class CorruptedMiddleScenario:
    name = "corrupted_jsonl_middle"

    def setup(self, runtime) -> None:
        runtime.state_file.write_text("{}", encoding="utf-8")
        runtime.events_file.write_text(
            json.dumps({"event": "checkpoint_advanced", "checkpoint": "cp-1", "at": "2026-01-01T00:00:00+00:00"})
            + "\n{bad}\n"
            + json.dumps({"event": "checkpoint_advanced", "checkpoint": "cp-2", "at": "2026-01-01T00:00:01+00:00"})
            + "\n",
            encoding="utf-8",
        )
        runtime.runs_file.write_text("", encoding="utf-8")

    def verify(self, runtime) -> dict:
        code, report = build_doctor_report(runtime)
        return {
            "ok": code == 1,
            "status": report["status"],
            "events_readable": next(
                c["ok"] for c in report["checks"] if c["name"] == "events_jsonl_readable"
            ),
        }
