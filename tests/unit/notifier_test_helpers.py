from __future__ import annotations

from pathlib import Path

from amazon_notify.runtime import RuntimeConfig


def build_runtime(
    tmp_path: Path,
    *,
    dry_run: bool = False,
    **overrides: object,
) -> RuntimeConfig:
    config_data: dict[str, object] = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "amazon_from_pattern": r"amazon\.co\.jp",
        "state_file": tmp_path / "state.json",
        "events_file": tmp_path / "events.jsonl",
        "runs_file": tmp_path / "runs.jsonl",
        "max_messages": 10,
    }
    config_data.update(overrides)
    return RuntimeConfig.from_mapping(config_data, dry_run=dry_run)


def single_page(messages: list[dict[str, str]]):
    def _page(_service, *, query: str, max_results: int, page_token: str | None = None):
        assert query == "in:inbox"
        _ = max_results
        if page_token is not None:
            return [], None
        return messages, None

    return _page

