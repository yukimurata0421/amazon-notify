from __future__ import annotations

from ..runtime import RuntimeConfig
from ..verify_state import run_verify_state as run_verify_state_impl


def run_verify_state(runtime: RuntimeConfig) -> tuple[int, dict]:
    return run_verify_state_impl(runtime)
