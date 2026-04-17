from __future__ import annotations

import argparse
import json
import sys

from ..checkpoint_store import JsonlCheckpointStore
from ..runtime import RuntimeConfig
from . import status as status_command


def handle_rebuild_indexes(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.rebuild_indexes:
        return False
    store = JsonlCheckpointStore(
        state_file=runtime.state_file,
        events_file=runtime.events_file,
        runs_file=runtime.runs_file,
    )
    rebuilt = store.rebuild_indexes()
    sys.stdout.write(
        json.dumps(
            {
                "status": "ok",
                "checkpoint_index_rebuilt": rebuilt["checkpoint_index"],
                "run_summary_index_rebuilt": rebuilt["run_summary_index"],
                "events_file": str(runtime.events_file),
                "runs_file": str(runtime.runs_file),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    return True


def handle_status_report(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.status:
        return False
    exit_code, report = status_command.build_status_report(runtime)
    sys.stdout.write(status_command.format_status_summary(report) + "\n")
    raise SystemExit(exit_code)


def handle_doctor_report(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.doctor:
        return False
    exit_code, report = status_command.build_doctor_report(runtime)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(exit_code)


def handle_verify_state_report(
    args: argparse.Namespace, runtime: RuntimeConfig
) -> bool:
    if not args.verify_state:
        return False
    exit_code, report = status_command.build_doctor_report(runtime)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(exit_code)


def handle_metrics_report(args: argparse.Namespace, runtime: RuntimeConfig) -> bool:
    if not args.metrics:
        return False
    report = status_command.build_metrics_report(
        runtime, recent_run_window=args.metrics_window
    )
    if args.metrics_plain:
        sys.stdout.write(status_command.format_metrics_plain(report) + "\n")
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0)
