from pathlib import Path

import pytest

from amazon_notify import runtime
from amazon_notify.config import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=tmp_path,
        config=tmp_path / "config.json",
        credentials=tmp_path / "credentials.json",
        token=tmp_path / "token.json",
        default_log=tmp_path / "logs" / "amazon_mail_notifier.log",
    )


def test_build_runtime_defaults_and_mapping_api(tmp_path: Path) -> None:
    paths = _runtime_paths(tmp_path)
    config = {"discord_webhook_url": "https://discord.com/api/webhooks/1/token"}

    built = runtime.build_runtime(config, dry_run=True, paths=paths)

    assert built["amazon_pattern"].pattern == r"amazon\.co\.jp"
    assert built.get("missing_key", "fallback") == "fallback"
    assert built.events_file == tmp_path / "events.jsonl"
    assert built.runs_file == tmp_path / "runs.jsonl"
    assert built.discord_dedupe_state_file == tmp_path / ".discord_dedupe_state.json"
    assert built.runtime_paths.runtime_dir == tmp_path
    assert built.subject_pattern is None


def test_build_runtime_raises_for_invalid_amazon_from_pattern(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        runtime.build_runtime(
            {
                "discord_webhook_url": "https://discord.com/api/webhooks/1/token",
                "amazon_from_pattern": "[",
            },
            dry_run=True,
            paths=_runtime_paths(tmp_path),
        )


def test_compile_optional_pattern_and_discord_webhook_shape() -> None:
    compiled = runtime.compile_optional_pattern(
        r"(発送|配達済み)", "amazon_subject_pattern"
    )
    assert compiled is not None
    assert compiled.search("発送のお知らせ")
    assert runtime.compile_optional_pattern("", "amazon_subject_pattern") is None
    with pytest.raises(ValueError):
        runtime.compile_optional_pattern("[", "amazon_subject_pattern")

    assert runtime.looks_like_discord_webhook_url(
        "https://discord.com/api/webhooks/1/token"
    )
    assert not runtime.looks_like_discord_webhook_url(
        "http://discord.com/api/webhooks/1/token"
    )
    assert not runtime.looks_like_discord_webhook_url(
        "https://example.com/api/webhooks/1/token"
    )
    assert not runtime.looks_like_discord_webhook_url("https://discord.com/channels/1")


def test_validate_config_reports_numeric_and_retry_errors(tmp_path: Path) -> None:
    errors = runtime.validate_config(
        {
            "discord_webhook_url": "",
            "max_messages": "x",
            "poll_interval_seconds": 5,
            "gmail_api_max_retries": "x",
            "discord_max_retries": 0,
            "pubsub_trigger_failure_max_consecutive": 0,
            "pubsub_stream_reconnect_max_attempts": -1,
            "gmail_api_base_delay_seconds": "x",
            "gmail_api_max_delay_seconds": 0,
            "pubsub_subscription": "   ",
            "pubsub_main_service_name": "   ",
            "pubsub_heartbeat_interval_seconds": "x",
            "pubsub_heartbeat_max_age_seconds": 0,
            "structured_logging": "yes",
            "amazon_from_pattern": "[",
            "amazon_subject_pattern": "(",
            "state_file": 123,
            "log_file": "",
        },
        paths=_runtime_paths(tmp_path),
    )

    joined = "\n".join(errors)
    assert "discord_webhook_url" in joined
    assert "max_messages は整数" in joined
    assert "poll_interval_seconds は 10 以上" in joined
    assert "gmail_api_max_retries は整数" in joined
    assert "discord_max_retries は 1 以上" in joined
    assert "pubsub_trigger_failure_max_consecutive は 1 以上" in joined
    assert "pubsub_stream_reconnect_max_attempts は 0 以上" in joined
    assert "gmail_api_base_delay_seconds は数値" in joined
    assert "gmail_api_max_delay_seconds は 0 より大きい値" in joined
    assert "pubsub_subscription は空文字以外" in joined
    assert "pubsub_main_service_name は空文字以外" in joined
    assert "pubsub_heartbeat_interval_seconds は数値" in joined
    assert "pubsub_heartbeat_max_age_seconds は 0 より大きい値" in joined
    assert "structured_logging は true/false" in joined
    assert "amazon_from_pattern の正規表現が不正" in joined
    assert "amazon_subject_pattern の正規表現が不正" in joined
    assert "state_file は空文字以外の文字列" in joined
    assert "log_file は空文字以外の文字列" in joined


def test_validate_config_reports_runtime_path_resolution_failures(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_resolve_runtime_path(path_value, base_dir=None):
        if str(path_value) == "raise-path":
            raise ValueError("boom")
        base = tmp_path if base_dir is None else base_dir
        return base / str(path_value)

    monkeypatch.setattr(
        runtime.app_config, "resolve_runtime_path", fake_resolve_runtime_path
    )
    errors = runtime.validate_config(
        {
            "discord_webhook_url": "https://discord.com/api/webhooks/1/token",
            "pubsub_stream_reconnect_max_attempts": "x",
            "pubsub_heartbeat_file": "raise-path",
            "events_file": "raise-path",
        },
        paths=_runtime_paths(tmp_path),
    )

    joined = "\n".join(errors)
    assert "pubsub_stream_reconnect_max_attempts は整数" in joined
    assert "pubsub_heartbeat_file を runtime パスとして解決できません" in joined
    assert "events_file を runtime パスとして解決できません" in joined


def test_validate_config_transient_alert_thresholds_allow_zero_and_reject_negative(
    tmp_path: Path,
) -> None:
    ok_errors = runtime.validate_config(
        {
            "discord_webhook_url": "https://discord.com/api/webhooks/1/token",
            "transient_alert_min_duration_seconds": 0.0,
            "transient_alert_cooldown_seconds": 0.0,
        },
        paths=_runtime_paths(tmp_path),
    )
    assert not any("transient_alert_" in err for err in ok_errors)

    ng_errors = runtime.validate_config(
        {
            "discord_webhook_url": "https://discord.com/api/webhooks/1/token",
            "transient_alert_min_duration_seconds": -1,
            "transient_alert_cooldown_seconds": -2,
        },
        paths=_runtime_paths(tmp_path),
    )
    joined = "\n".join(ng_errors)
    assert "transient_alert_min_duration_seconds は 0 以上" in joined
    assert "transient_alert_cooldown_seconds は 0 以上" in joined
