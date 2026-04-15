#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import sys

import requests


def _systemctl_result(unit_name: str) -> str:
    proc = subprocess.run(
        ["systemctl", "show", "-p", "Result", "--value", unit_name],
        check=False,
        capture_output=True,
        text=True,
    )
    value = (proc.stdout or proc.stderr or "").strip()
    return value or "unknown"


def main() -> int:
    unit_name = sys.argv[1] if len(sys.argv) > 1 else "amazon-notify.service"
    webhook_url = os.environ.get("DISCORD_ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_ALERT_WEBHOOK_URL is empty; skip alert.")
        return 0

    unit_result = _systemctl_result(unit_name)
    if unit_result != "start-limit-hit":
        print(f"Unit result is '{unit_result}'; skip alert.")
        return 0

    host_name = socket.gethostname()
    timestamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    content = (
        "amazon-notify restart storm detected on "
        f"{host_name} at {timestamp}. unit={unit_name} result={unit_result}"
    )

    try:
        response = requests.post(
            webhook_url,
            json={"content": content},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"Failed to send discord alert: {exc}", file=sys.stderr)
        return 1

    if 200 <= response.status_code < 300:
        return 0
    print(
        f"Discord alert failed: status={response.status_code} body={response.text}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
