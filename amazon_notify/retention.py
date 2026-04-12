from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .checkpoint_store import JsonlCheckpointStore
from .metrics import build_metrics_report
from .runtime import RuntimeConfig
from .status import build_status_report
from .verify_state import run_verify_state


@dataclass(frozen=True)
class ArchiveOptions:
    label: str | None = None
    archive_dir: Path | None = None
    gzip_enabled: bool = True


@dataclass(frozen=True)
class RestoreOptions:
    label: str
    archive_dir: Path | None = None


@dataclass(frozen=True)
class DrillOptions:
    archive_dir: Path | None = None


def archive_runtime(runtime: RuntimeConfig, *, options: ArchiveOptions) -> dict[str, Any]:
    label = options.label or _default_label()
    archive_dir = (
        options.archive_dir
        if options.archive_dir is not None
        else runtime.runtime_paths.runtime_dir / "archive"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, Any]] = []
    for source, logical_name in _runtime_targets(runtime):
        copied.append(
            _archive_file(
                source=source,
                archive_dir=archive_dir,
                logical_name=logical_name,
                label=label,
                gzip_enabled=options.gzip_enabled,
            )
        )

    manifest_path = archive_dir / f"manifest-{label}.json"
    manifest = {
        "label": label,
        "created_at": datetime.now(UTC).isoformat(),
        "archive_dir": str(archive_dir),
        "files": copied,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "label": label,
        "archive_dir": str(archive_dir),
        "manifest": str(manifest_path),
        "files": copied,
    }


def restore_runtime(runtime: RuntimeConfig, *, options: RestoreOptions) -> tuple[int, dict[str, Any]]:
    archive_dir = (
        options.archive_dir
        if options.archive_dir is not None
        else runtime.runtime_paths.runtime_dir / "archive"
    )
    label = options.label

    restored: list[dict[str, Any]] = []
    for destination, logical_name in _runtime_targets(runtime):
        restored.append(
            _restore_file(
                destination=destination,
                archive_dir=archive_dir,
                logical_name=logical_name,
                label=label,
            )
        )

    store = JsonlCheckpointStore(
        state_file=runtime.state_file,
        events_file=runtime.events_file,
        runs_file=runtime.runs_file,
    )
    rebuilt = store.rebuild_indexes()

    verify_code, verify_report = run_verify_state(runtime)
    return verify_code, {
        "status": "ok" if verify_code == 0 else "degraded",
        "label": label,
        "archive_dir": str(archive_dir),
        "restored": restored,
        "indexes_rebuilt": rebuilt,
        "verify_state_status": verify_report.get("status"),
    }


def run_restore_drill(runtime: RuntimeConfig, *, options: DrillOptions) -> tuple[int, dict[str, Any]]:
    with TemporaryDirectory(prefix="amazon-notify-drill-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        drill_dir = temp_dir / "runtime"
        archive_dir = (
            options.archive_dir
            if options.archive_dir is not None
            else temp_dir / "archive"
        )
        drill_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)

        for source, logical_name in _runtime_targets(runtime):
            destination = drill_dir / logical_name
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        simulated = RuntimeConfig.from_mapping(
            {
                "discord_webhook_url": runtime.discord_webhook_url,
                "state_file": str(drill_dir / "state.json"),
                "events_file": str(drill_dir / "events.jsonl"),
                "runs_file": str(drill_dir / "runs.jsonl"),
            },
            dry_run=True,
            paths=runtime.runtime_paths,
        )

        archive_report = archive_runtime(
            simulated,
            options=ArchiveOptions(label="drill", archive_dir=archive_dir, gzip_enabled=True),
        )

        # index を意図的に削除して rebuild/restore 耐性を確認
        for idx_file in (
            simulated.events_file.with_name(f"{simulated.events_file.name}.checkpoint.index.json"),
            simulated.runs_file.with_name(f"{simulated.runs_file.name}.summary.index.json"),
        ):
            idx_file.unlink(missing_ok=True)

        restore_code, restore_report = restore_runtime(
            simulated,
            options=RestoreOptions(label="drill", archive_dir=archive_dir),
        )
        status_code, status_report = build_status_report(simulated)
        verify_code, verify_report = run_verify_state(simulated)
        metrics = build_metrics_report(simulated)

        ok = restore_code == 0 and status_code == 0 and verify_code == 0
        report = {
            "status": "ok" if ok else "degraded",
            "archive": archive_report,
            "restore": restore_report,
            "status_report": status_report,
            "verify_report": verify_report.get("status"),
            "metrics": metrics,
        }
        return (0 if ok else 1), report


def _runtime_targets(runtime: RuntimeConfig) -> list[tuple[Path, str]]:
    return [
        (runtime.events_file, "events.jsonl"),
        (runtime.runs_file, "runs.jsonl"),
        (runtime.state_file, "state.json"),
        (
            runtime.events_file.with_name(f"{runtime.events_file.name}.checkpoint.index.json"),
            "events.jsonl.checkpoint.index.json",
        ),
        (
            runtime.runs_file.with_name(f"{runtime.runs_file.name}.summary.index.json"),
            "runs.jsonl.summary.index.json",
        ),
    ]


def _default_label() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _archive_file(
    *,
    source: Path,
    archive_dir: Path,
    logical_name: str,
    label: str,
    gzip_enabled: bool,
) -> dict[str, Any]:
    destination_base = archive_dir / f"{logical_name}-{label}"
    if not source.exists():
        return {
            "source": str(source),
            "archived": False,
            "reason": "missing",
        }

    if gzip_enabled:
        destination = archive_dir / f"{logical_name}-{label}.gz"
        with source.open("rb") as src, gzip.open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        destination = destination_base
        shutil.copy2(source, destination)

    return {
        "source": str(source),
        "destination": str(destination),
        "archived": True,
        "size": source.stat().st_size,
        "sha256": _sha256(source),
    }


def _restore_file(
    *,
    destination: Path,
    archive_dir: Path,
    logical_name: str,
    label: str,
) -> dict[str, Any]:
    plain = archive_dir / f"{logical_name}-{label}"
    gz = archive_dir / f"{logical_name}-{label}.gz"

    if not plain.exists() and not gz.exists():
        return {
            "destination": str(destination),
            "restored": False,
            "reason": "archive_missing",
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    if gz.exists():
        with gzip.open(gz, "rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        source_path = gz
    else:
        shutil.copy2(plain, destination)
        source_path = plain

    return {
        "destination": str(destination),
        "source": str(source_path),
        "restored": True,
    }


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()
