from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from ..config import RuntimePaths
from ..runtime import RuntimeConfig


class Scenario(Protocol):
    name: str

    def setup(self, runtime: RuntimeConfig) -> None: ...

    def verify(self, runtime: RuntimeConfig) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    ok: bool
    details: dict[str, Any]


_SCENARIOS: dict[str, Scenario] = {}


def register_scenario(name: str) -> Callable[[Scenario], Scenario]:
    def _decorator(scenario: Scenario) -> Scenario:
        registered = scenario() if isinstance(scenario, type) else scenario
        _SCENARIOS[name] = registered
        return scenario

    return _decorator


def list_scenarios() -> list[str]:
    return sorted(_SCENARIOS.keys())


def get_scenario(name: str) -> Scenario | None:
    return _SCENARIOS.get(name)


def _clone_runtime(runtime: RuntimeConfig, runtime_dir: Path) -> RuntimeConfig:
    cfg = {
        "discord_webhook_url": runtime.discord_webhook_url,
        "amazon_from_pattern": runtime.amazon_pattern.pattern,
        "state_file": "state.json",
        "events_file": "events.jsonl",
        "runs_file": "runs.jsonl",
    }
    return RuntimeConfig.from_mapping(cfg, paths=RuntimePaths(
        runtime_dir=runtime_dir,
        config=runtime_dir / "config.json",
        credentials=runtime_dir / "credentials.json",
        token=runtime_dir / "token.json",
        default_log=runtime_dir / "logs" / "amazon_mail_notifier.log",
    ))


def run_scenarios(runtime: RuntimeConfig, names: list[str] | None = None) -> list[ScenarioResult]:
    selected = names or list_scenarios()
    results: list[ScenarioResult] = []

    for name in selected:
        scenario = get_scenario(name)
        if scenario is None:
            results.append(
                ScenarioResult(
                    name=name,
                    ok=False,
                    details={"error": f"unknown scenario: {name}"},
                )
            )
            continue

        with TemporaryDirectory(prefix=f"scenario-{name}-") as tmp:
            rt = _clone_runtime(runtime, Path(tmp))
            try:
                scenario.setup(rt)
                details = scenario.verify(rt)
                results.append(
                    ScenarioResult(
                        name=name,
                        ok=bool(details.get("ok", False)),
                        details=details,
                    )
                )
            except Exception as exc:
                results.append(
                    ScenarioResult(
                        name=name,
                        ok=False,
                        details={"error": str(exc)},
                    )
                )

    return results


# scenario registrations
from . import _checkpoint_interrupt_window  # noqa: E402,F401
from . import _corrupted_middle  # noqa: E402,F401
from . import _enospc  # noqa: E402,F401
from . import _stale_incident_recovery  # noqa: E402,F401
from . import _truncated_jsonl  # noqa: E402,F401
