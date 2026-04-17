from __future__ import annotations

import errno

from ..checkpoint_store import JsonlCheckpointStore
from ..domain import Checkpoint
from ..errors import CheckpointError
from . import register_scenario


@register_scenario("enospc_checkpoint")
class EnospcScenario:
    name = "enospc_checkpoint"

    def setup(self, runtime) -> None:
        runtime.state_file.write_text("{}", encoding="utf-8")
        runtime.events_file.write_text("", encoding="utf-8")
        runtime.runs_file.write_text("", encoding="utf-8")

    def verify(self, runtime) -> dict:
        store = JsonlCheckpointStore(
            state_file=runtime.state_file,
            events_file=runtime.events_file,
            runs_file=runtime.runs_file,
        )

        def _raise_enospc(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        store._append_jsonl = _raise_enospc  # type: ignore[method-assign]
        try:
            store.advance_checkpoint(Checkpoint(message_id="cp-1"), "run-1")
        except CheckpointError as exc:
            return {
                "ok": "ENOSPC" in str(exc) or "容量不足" in str(exc),
                "error": str(exc),
            }
        return {"ok": False, "error": "expected CheckpointError"}
