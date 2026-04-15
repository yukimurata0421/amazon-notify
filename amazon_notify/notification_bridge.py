"""Bridge between Gmail state management and Discord notification with deduplication.

This module owns the computation of dedupe state paths and wraps
``discord_client.send_discord_alert`` / ``send_discord_recovery`` with
dedupe-aware helpers.  Extracting these from ``gmail_client.py`` ensures
the Gmail boundary layer does not depend on Discord-specific concerns.
"""

from __future__ import annotations

from pathlib import Path

from .discord_client import send_discord_alert, send_discord_recovery

_DISCORD_DEDUPE_STATE_FILENAME = ".discord_dedupe_state.json"


def dedupe_state_path_for_state_file(state_file: Path) -> Path:
    return state_file.parent / _DISCORD_DEDUPE_STATE_FILENAME


def send_alert_with_dedupe(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None,
) -> bool:
    return send_discord_alert(
        webhook_url,
        message,
        dedupe_state_path=dedupe_state_path,
    )


def send_recovery_with_dedupe(
    webhook_url: str,
    message: str,
    *,
    dedupe_state_path: Path | None,
) -> bool:
    return send_discord_recovery(
        webhook_url,
        message,
        dedupe_state_path=dedupe_state_path,
    )
