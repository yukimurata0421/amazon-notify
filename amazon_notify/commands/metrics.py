from __future__ import annotations

from ..metrics import build_metrics_report as build_metrics_report_impl
from ..metrics import format_metrics_plain as format_metrics_plain_impl
from ..runtime import RuntimeConfig


def build_metrics_report(runtime: RuntimeConfig, *, window: int) -> dict:
    return build_metrics_report_impl(runtime, window=window)


def format_metrics_plain(metrics: dict) -> str:
    return format_metrics_plain_impl(metrics)
