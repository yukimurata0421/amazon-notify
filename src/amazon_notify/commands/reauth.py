from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from .. import config as app_config
from ..config import RuntimePaths


def handle_reauth(
    args: argparse.Namespace,
    *,
    paths: RuntimePaths,
    run_oauth_flow_fn: Callable[..., object | None],
) -> bool:
    if not args.reauth:
        return False

    log_path = (
        app_config.resolve_runtime_path(args.log_file, base_dir=paths.runtime_dir)
        if args.log_file
        else paths.default_log
    )
    app_config.setup_logging(log_path)
    app_config.LOGGER.info("MANUAL_REAUTH_START")

    creds = run_oauth_flow_fn(paths=paths)
    if not creds:
        app_config.LOGGER.error("MANUAL_REAUTH_FAILED")
        sys.exit(1)

    app_config.LOGGER.info("MANUAL_REAUTH_SUCCESS")
    return True
