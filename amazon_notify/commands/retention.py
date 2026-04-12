from __future__ import annotations

from pathlib import Path

from ..retention import (
    ArchiveOptions,
    DrillOptions,
    RestoreOptions,
)
from ..retention import (
    archive_runtime as archive_runtime_impl,
)
from ..retention import restore_runtime as restore_runtime_impl
from ..retention import run_restore_drill as run_restore_drill_impl
from ..runtime import RuntimeConfig


def archive_runtime(
    runtime: RuntimeConfig,
    *,
    label: str | None,
    archive_dir: Path | None,
    gzip_enabled: bool,
) -> dict:
    return archive_runtime_impl(
        runtime,
        options=ArchiveOptions(
            label=label,
            archive_dir=archive_dir,
            gzip_enabled=gzip_enabled,
        ),
    )


def restore_runtime(
    runtime: RuntimeConfig,
    *,
    label: str,
    archive_dir: Path | None,
) -> tuple[int, dict]:
    return restore_runtime_impl(
        runtime,
        options=RestoreOptions(label=label, archive_dir=archive_dir),
    )


def run_restore_drill(
    runtime: RuntimeConfig,
    *,
    archive_dir: Path | None,
) -> tuple[int, dict]:
    return run_restore_drill_impl(runtime, options=DrillOptions(archive_dir=archive_dir))
