from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from .. import config as app_config
from ..config import RuntimePaths
from ..domain import AuthStatus


def handle_setup_watch(
    args: argparse.Namespace,
    config: dict,
    *,
    paths: RuntimePaths,
    stderr_error: Callable[[str], None],
    load_state_fn: Callable[..., dict],
    get_gmail_service_with_status_fn: Callable[..., tuple[object | None, AuthStatus]],
    start_gmail_watch_with_retry_fn: Callable[..., dict],
) -> None:
    topic_name = (args.pubsub_topic or "").strip()
    if not topic_name:
        stderr_error("--setup-watch には --pubsub-topic が必要です。")
        sys.exit(1)

    state_file = app_config.resolve_runtime_path(
        config.get("state_file", "state.json"),
        base_dir=paths.runtime_dir,
    )
    state = load_state_fn(state_file)
    service, status = get_gmail_service_with_status_fn(
        webhook_url=config["discord_webhook_url"],
        state=state,
        state_file=state_file,
        allow_oauth_interactive=False,
        paths=paths,
    )
    if service is None:
        stderr_error(f"Gmail service を初期化できませんでした。auth_status={status.value}")
        sys.exit(1)

    label_ids = [item.strip() for item in args.watch_label_ids.split(",") if item.strip()]
    try:
        watch_response = start_gmail_watch_with_retry_fn(
            service,
            topic_name=topic_name,
            label_ids=label_ids,
            label_filter_action=args.watch_label_filter_action,
            retries=args.watch_retries,
            base_delay=args.watch_base_delay,
            max_delay=args.watch_max_delay,
        )
    except Exception as exc:
        app_config.LOGGER.error("GMAIL_WATCH_SETUP_FAILED: %s", exc)
        stderr_error(f"Gmail watch 登録に失敗しました: {exc}")
        sys.exit(1)

    sys.stdout.write(json.dumps(watch_response, ensure_ascii=False, indent=2) + "\n")
