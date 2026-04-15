#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from amazon_notify.failover import evaluate_main_health


def main() -> int:
    base_dir = Path(__file__).resolve().parents[2]
    main_service_name = os.environ.get("MAIN_SERVICE_NAME", "amazon-notify-pubsub.service")
    heartbeat_file = Path(
        os.environ.get("HEARTBEAT_FILE", str(base_dir / "runtime/pubsub-heartbeat.txt"))
    )
    try:
        heartbeat_max_age_seconds = float(
            os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "300")
        )
    except ValueError:
        print("invalid HEARTBEAT_MAX_AGE_SECONDS", file=sys.stderr)
        return 1

    health = evaluate_main_health(
        service_name=main_service_name,
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=heartbeat_max_age_seconds,
    )
    if health.healthy:
        print(f"watchdog health ok: {health.reason}", file=sys.stderr)
        return 0

    print(f"watchdog restarting {main_service_name}: {health.reason}", file=sys.stderr)
    return subprocess.run(
        ["systemctl", "restart", main_service_name],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
