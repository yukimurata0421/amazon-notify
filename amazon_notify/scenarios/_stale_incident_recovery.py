from __future__ import annotations

import json

from ..status import build_doctor_report
from . import register_scenario


@register_scenario("stale_incident_recovery")
class StaleIncidentRecoveryScenario:
    name = "stale_incident_recovery"

    def setup(self, runtime) -> None:
        runtime.state_file.write_text(
            json.dumps(
                {
                    "active_incident_kind": "delivery_failed",
                    "active_incident_message": "boom",
                    "active_incident_at": "2026-01-01T00:00:00+00:00",
                    "incident_suppressed_count": 0,
                }
            ),
            encoding="utf-8",
        )
        runtime.events_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "incident_recovered",
                    "run_id": "run-2",
                    "at": "2026-01-01T00:00:10+00:00",
                    "kind": "delivery_failed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runtime.runs_file.write_text("", encoding="utf-8")

    def verify(self, runtime) -> dict:
        code, report = build_doctor_report(runtime)
        incident_check = next(
            c for c in report["checks"] if c["name"] == "incident_lifecycle_consistent"
        )
        return {
            "ok": (code == 1) and (not incident_check["ok"]),
            "incident_detail": incident_check["detail"],
        }
