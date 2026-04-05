from __future__ import annotations

from typing import Callable

from .. import config as app_config
from ..health import load_config_for_health_check as load_config_for_health_check_impl
from ..health import run_health_check as run_health_check_impl


def load_config_for_health_check(
    *,
    validate_config: Callable[[dict], list[str]],
) -> tuple[dict | None, list[str]]:
    return load_config_for_health_check_impl(
        app_config.get_runtime_paths(),
        validate_config=validate_config,
    )


def run_health_check(
    *,
    config: dict | None,
    validation_errors: list[str],
) -> tuple[int, dict]:
    return run_health_check_impl(
        app_config.get_runtime_paths(),
        config=config,
        validation_errors=validation_errors,
    )
