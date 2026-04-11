from __future__ import annotations

from ..runtime import RuntimeConfig
from ..status import (
    build_doctor_report as build_doctor_report_impl,
)
from ..status import (
    build_metrics_report as build_metrics_report_impl,
)
from ..status import (
    build_status_report as build_status_report_impl,
)
from ..status import (
    format_metrics_plain as format_metrics_plain_impl,
)
from ..status import (
    format_status_summary as format_status_summary_impl,
)


def build_status_report(runtime: RuntimeConfig) -> tuple[int, dict]:
    return build_status_report_impl(runtime)


def build_doctor_report(runtime: RuntimeConfig) -> tuple[int, dict]:
    return build_doctor_report_impl(runtime)


def build_metrics_report(
    runtime: RuntimeConfig, *, recent_run_window: int
) -> dict:
    return build_metrics_report_impl(
        runtime, recent_run_window=recent_run_window
    )


def format_status_summary(report: dict) -> str:
    return format_status_summary_impl(report)


def format_metrics_plain(report: dict) -> str:
    return format_metrics_plain_impl(report)
