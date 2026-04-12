from __future__ import annotations

from ..runtime import RuntimeConfig
from ..scenario_harness import run_scenario_harness as run_scenario_harness_impl


def run_scenario_harness(
    runtime: RuntimeConfig,
    *,
    scenario_names: list[str] | None,
) -> tuple[int, dict]:
    return run_scenario_harness_impl(runtime, scenario_names=scenario_names)
