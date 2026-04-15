#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _repo_base() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_topic_from_config(config_path: Path) -> str:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    topic = payload.get("pubsub_topic")
    return topic.strip() if isinstance(topic, str) else ""


def main() -> int:
    base_dir = _repo_base()
    amazon_notify_bin = Path(
        os.environ.get("AMAZON_NOTIFY_BIN", str(base_dir / ".venv/bin/amazon-notify"))
    )
    config_path = Path(os.environ.get("CONFIG_PATH", str(base_dir / "config.json")))
    pubsub_topic = os.environ.get("PUBSUB_TOPIC", "").strip()
    watch_label_ids = os.environ.get("WATCH_LABEL_IDS", "INBOX")
    watch_label_filter_action = os.environ.get("WATCH_LABEL_FILTER_ACTION", "include")
    watch_retries = os.environ.get("WATCH_RETRIES", "4")
    watch_base_delay = os.environ.get("WATCH_BASE_DELAY", "1.0")
    watch_max_delay = os.environ.get("WATCH_MAX_DELAY", "60.0")

    if not amazon_notify_bin.exists():
        print(f"amazon-notify binary not found: {amazon_notify_bin}", file=sys.stderr)
        return 1
    if not config_path.exists():
        print(f"config file not found: {config_path}", file=sys.stderr)
        return 1

    if not pubsub_topic:
        pubsub_topic = _read_topic_from_config(config_path)
    if not pubsub_topic:
        print(
            "pubsub topic is empty. Set PUBSUB_TOPIC env or config.json pubsub_topic.",
            file=sys.stderr,
        )
        return 1

    cmd = [
        str(amazon_notify_bin),
        "--config",
        str(config_path),
        "--setup-watch",
        "--pubsub-topic",
        pubsub_topic,
        "--watch-label-ids",
        watch_label_ids,
        "--watch-label-filter-action",
        watch_label_filter_action,
        "--watch-retries",
        watch_retries,
        "--watch-base-delay",
        watch_base_delay,
        "--watch-max-delay",
        watch_max_delay,
    ]
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
