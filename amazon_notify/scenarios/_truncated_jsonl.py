from __future__ import annotations

from ..checkpoint_store import JsonlCheckpointStore
from ..domain import Checkpoint
from ..status import build_doctor_report
from . import register_scenario


@register_scenario("truncated_jsonl")
class TruncatedJsonlScenario:
    name = "truncated_jsonl"

    def setup(self, runtime) -> None:
        runtime.state_file.write_text("{}", encoding="utf-8")
        store = JsonlCheckpointStore(
            state_file=runtime.state_file,
            events_file=runtime.events_file,
            runs_file=runtime.runs_file,
        )
        store.advance_checkpoint(Checkpoint(message_id="cp-1"), "run-1")
        good = runtime.events_file.read_text(encoding="utf-8")
        runtime.events_file.write_text(
            good + '{"schema_version":1,"event":"checkpoint_advanced"\n',
            encoding="utf-8",
        )

    def verify(self, runtime) -> dict:
        code, report = build_doctor_report(runtime)
        return {
            "ok": code == 0,
            "status": report["status"],
            "tail_corruption_ignored": report["runtime_status"][
                "tail_corruption_ignored"
            ]["events_jsonl"],
        }
