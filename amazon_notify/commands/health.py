from __future__ import annotations

from collections.abc import Callable

from ..config import RuntimePaths
from ..health import load_config_for_health_check as load_config_for_health_check_impl
from ..health import run_health_check as run_health_check_impl


def load_config_for_health_check(
    paths: RuntimePaths,
    *,
    validate_config: Callable[[dict], list[str]],
) -> tuple[dict | None, list[str]]:
    return load_config_for_health_check_impl(
        paths,
        validate_config=validate_config,
    )


def run_health_check(
    paths: RuntimePaths,
    *,
    config: dict | None,
    validation_errors: list[str],
) -> tuple[int, dict]:
    return run_health_check_impl(
        paths,
        config=config,
        validation_errors=validation_errors,
    )
