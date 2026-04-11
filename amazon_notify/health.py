from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from . import config as app_config
from .checkpoint_store import JsonlCheckpointStore
from .config import RuntimePaths
from .discord_client import has_dedupe_file_lock_support
from .errors import CheckpointError
from .runtime import DEFAULT_EVENTS_FILE_RELATIVE, DEFAULT_RUNS_FILE_RELATIVE
from .time_utils import utc_now_iso


def load_config_for_health_check(
    paths: RuntimePaths,
    *,
    validate_config: Callable[[dict], list[str]],
) -> tuple[dict | None, list[str]]:
    if not paths.config.exists():
        return None, [f"{paths.config} が見つかりません。"]

    try:
        config = app_config.load_config(paths.config)
    except json.JSONDecodeError as exc:
        return None, [f"config.json の JSON が不正です: {exc}"]
    except OSError as exc:
        return None, [f"config.json を読み込めませんでした: {exc}"]

    return config, validate_config(config)


def run_health_check(
    paths: RuntimePaths,
    *,
    config: dict | None,
    validation_errors: list[str],
) -> tuple[int, dict]:
    checks: list[dict[str, str | bool]] = []

    checks.append(
        {
            "name": "config_file_exists",
            "ok": paths.config.exists(),
            "detail": str(paths.config),
        }
    )
    checks.append(
        {
            "name": "config_valid",
            "ok": not validation_errors,
            "detail": "OK" if not validation_errors else " / ".join(validation_errors),
        }
    )
    checks.append(
        {
            "name": "credentials_file_exists",
            "ok": paths.credentials.exists(),
            "detail": str(paths.credentials),
        }
    )
    checks.append(
        {
            "name": "token_file_exists",
            "ok": paths.token.exists(),
            "detail": str(paths.token),
        }
    )
    lock_supported = has_dedupe_file_lock_support()
    checks.append(
        {
            "name": "dedupe_lock_supported",
            "ok": lock_supported,
            "detail": "fcntl available" if lock_supported else "fcntl unavailable",
        }
    )

    state_file: Path | None = None
    log_file: Path | None = None
    events_file: Path | None = None
    runs_file: Path | None = None
    last_run_summary: dict | None = None
    active_incident: dict | None = None
    if config is not None:
        state_file = app_config.resolve_runtime_path(
            config.get("state_file", "state.json"), base_dir=paths.runtime_dir
        )
        log_file = app_config.resolve_runtime_path(
            config.get("log_file", str(paths.default_log)),
            base_dir=paths.runtime_dir,
        )
        events_file = app_config.resolve_runtime_path(
            config.get("events_file", DEFAULT_EVENTS_FILE_RELATIVE),
            base_dir=paths.runtime_dir,
        )
        runs_file = app_config.resolve_runtime_path(
            config.get("runs_file", DEFAULT_RUNS_FILE_RELATIVE),
            base_dir=paths.runtime_dir,
        )
        try:
            checkpoint_store = JsonlCheckpointStore(
                state_file=state_file,
                events_file=events_file,
                runs_file=runs_file,
            )
            last_run_summary = checkpoint_store.load_last_run_summary()
            active_incident = checkpoint_store.load_incident_state()
        except CheckpointError as exc:
            checks.append(
                {
                    "name": "runtime_records_valid",
                    "ok": False,
                    "detail": str(exc),
                }
            )

    status = "ok" if all(bool(check["ok"]) for check in checks) else "degraded"
    report = {
        "status": status,
        "checked_at": utc_now_iso(),
        "runtime_dir": str(paths.runtime_dir),
        "config_path": str(paths.config),
        "state_file": str(state_file) if state_file else None,
        "log_file": str(log_file) if log_file else None,
        "events_file": str(events_file) if events_file else None,
        "runs_file": str(runs_file) if runs_file else None,
        "runtime_status": {
            "last_run_status": (last_run_summary or {}).get("last_run_status"),
            "last_failure_kind": (last_run_summary or {}).get("last_failure_kind"),
            "checkpoint_before": (last_run_summary or {}).get("checkpoint_before"),
            "checkpoint_after": (last_run_summary or {}).get("checkpoint_after"),
            "last_success_at": (last_run_summary or {}).get("last_success_at"),
            "auth_status": (last_run_summary or {}).get("auth_status"),
            "active_incident": active_incident,
        },
        "checks": checks,
    }
    return (0 if status == "ok" else 1), report
