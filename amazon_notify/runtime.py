from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from re import Pattern
from typing import Any
from urllib.parse import urlparse

from . import config as app_config
from .config import RuntimePaths

DEFAULT_LOG_FILE_RELATIVE = "logs/amazon_mail_notifier.log"
DEFAULT_EVENTS_FILE_RELATIVE = "events.jsonl"
DEFAULT_RUNS_FILE_RELATIVE = "runs.jsonl"
DEFAULT_TRANSIENT_STATE_FILE_RELATIVE = "transient_state.json"
DEFAULT_DISCORD_DEDUPE_STATE_FILE_RELATIVE = ".discord_dedupe_state.json"
DEFAULT_PUBSUB_HEARTBEAT_FILE_RELATIVE = "runtime/pubsub-heartbeat.txt"
DEFAULT_SERVICE_STATUS_FILE_RELATIVE = "runtime/amazon-notify-status.json"
MIN_POLL_INTERVAL_SECONDS = 10


@dataclass(frozen=True)
class GmailApiConfig:
    max_retries: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0


@dataclass(frozen=True)
class DiscordRetryConfig:
    max_retries: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0


@dataclass(frozen=True)
class PubSubConfig:
    main_service_name: str = "amazon-notify-pubsub.service"
    heartbeat_file: Path = Path(DEFAULT_PUBSUB_HEARTBEAT_FILE_RELATIVE)
    heartbeat_interval_seconds: float = 30.0
    heartbeat_max_age_seconds: float = 300.0
    trigger_failure_max_consecutive: int = 5
    trigger_failure_base_delay_seconds: float = 1.0
    trigger_failure_max_delay_seconds: float = 60.0
    stream_reconnect_base_delay_seconds: float = 1.0
    stream_reconnect_max_delay_seconds: float = 60.0
    stream_reconnect_max_attempts: int = 0
    idle_trigger_interval_seconds: float = 300.0


@dataclass(frozen=True)
class TransientAlertConfig:
    min_duration_seconds: float = 600.0
    cooldown_seconds: float = 1800.0


_FLAT_ATTR_MAP: dict[str, tuple[str, str]] = {
    "gmail_api_max_retries": ("gmail_api", "max_retries"),
    "gmail_api_base_delay_seconds": ("gmail_api", "base_delay_seconds"),
    "gmail_api_max_delay_seconds": ("gmail_api", "max_delay_seconds"),
    "discord_max_retries": ("discord_retry", "max_retries"),
    "discord_base_delay_seconds": ("discord_retry", "base_delay_seconds"),
    "discord_max_delay_seconds": ("discord_retry", "max_delay_seconds"),
    "pubsub_main_service_name": ("pubsub", "main_service_name"),
    "pubsub_heartbeat_file": ("pubsub", "heartbeat_file"),
    "pubsub_heartbeat_interval_seconds": ("pubsub", "heartbeat_interval_seconds"),
    "pubsub_heartbeat_max_age_seconds": ("pubsub", "heartbeat_max_age_seconds"),
    "pubsub_trigger_failure_max_consecutive": (
        "pubsub",
        "trigger_failure_max_consecutive",
    ),
    "pubsub_trigger_failure_base_delay_seconds": (
        "pubsub",
        "trigger_failure_base_delay_seconds",
    ),
    "pubsub_trigger_failure_max_delay_seconds": (
        "pubsub",
        "trigger_failure_max_delay_seconds",
    ),
    "pubsub_stream_reconnect_base_delay_seconds": (
        "pubsub",
        "stream_reconnect_base_delay_seconds",
    ),
    "pubsub_stream_reconnect_max_delay_seconds": (
        "pubsub",
        "stream_reconnect_max_delay_seconds",
    ),
    "pubsub_stream_reconnect_max_attempts": ("pubsub", "stream_reconnect_max_attempts"),
    "pubsub_idle_trigger_interval_seconds": ("pubsub", "idle_trigger_interval_seconds"),
    "transient_alert_min_duration_seconds": ("transient_alert", "min_duration_seconds"),
    "transient_alert_cooldown_seconds": ("transient_alert", "cooldown_seconds"),
}
_DEPRECATED_ATTR_WARNED: set[str] = set()


@dataclass(frozen=True)
class RuntimeConfig:
    discord_webhook_url: str
    amazon_pattern: Pattern[str]
    state_file: Path
    transient_state_file: Path
    events_file: Path
    runs_file: Path
    discord_dedupe_state_file: Path
    max_messages: int
    dry_run: bool
    gmail_api: GmailApiConfig
    discord_retry: DiscordRetryConfig
    pubsub: PubSubConfig
    transient_alert: TransientAlertConfig
    service_status_file: Path
    runtime_paths: RuntimePaths
    subject_pattern: Pattern[str] | None

    def __getattr__(self, name: str) -> Any:
        mapping = _FLAT_ATTR_MAP.get(name)
        if mapping is not None:
            if name not in _DEPRECATED_ATTR_WARNED:
                warnings.warn(
                    (
                        f"RuntimeConfig.{name} is deprecated; "
                        f"use RuntimeConfig.{mapping[0]}.{mapping[1]} instead"
                    ),
                    DeprecationWarning,
                    stacklevel=2,
                )
                _DEPRECATED_ATTR_WARNED.add(name)
            group_name, attr_name = mapping
            return getattr(object.__getattribute__(self, group_name), attr_name)
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    @classmethod
    def from_mapping(
        cls, config: dict, *, dry_run: bool = False, paths: RuntimePaths | None = None
    ) -> RuntimeConfig:
        runtime_paths = app_config.get_runtime_paths() if paths is None else paths
        base_dir = runtime_paths.runtime_dir
        return cls(
            discord_webhook_url=config["discord_webhook_url"],
            amazon_pattern=compile_required_pattern(
                str(config.get("amazon_from_pattern", r"amazon\.co\.jp")),
                "amazon_from_pattern",
            ),
            state_file=app_config.resolve_runtime_path(
                config.get("state_file", "state.json"), base_dir=base_dir
            ),
            transient_state_file=app_config.resolve_runtime_path(
                config.get("transient_state_file", DEFAULT_TRANSIENT_STATE_FILE_RELATIVE),
                base_dir=base_dir,
            ),
            events_file=app_config.resolve_runtime_path(
                config.get("events_file", DEFAULT_EVENTS_FILE_RELATIVE),
                base_dir=base_dir,
            ),
            runs_file=app_config.resolve_runtime_path(
                config.get("runs_file", DEFAULT_RUNS_FILE_RELATIVE),
                base_dir=base_dir,
            ),
            discord_dedupe_state_file=app_config.resolve_runtime_path(
                DEFAULT_DISCORD_DEDUPE_STATE_FILE_RELATIVE,
                base_dir=base_dir,
            ),
            max_messages=int(config.get("max_messages", 50)),
            dry_run=dry_run,
            gmail_api=GmailApiConfig(
                max_retries=int(config.get("gmail_api_max_retries", 4)),
                base_delay_seconds=float(
                    config.get("gmail_api_base_delay_seconds", 1.0)
                ),
                max_delay_seconds=float(
                    config.get("gmail_api_max_delay_seconds", 30.0)
                ),
            ),
            discord_retry=DiscordRetryConfig(
                max_retries=int(config.get("discord_max_retries", 4)),
                base_delay_seconds=float(config.get("discord_base_delay_seconds", 1.0)),
                max_delay_seconds=float(config.get("discord_max_delay_seconds", 30.0)),
            ),
            pubsub=PubSubConfig(
                main_service_name=str(
                    config.get(
                        "pubsub_main_service_name", "amazon-notify-pubsub.service"
                    )
                ),
                heartbeat_file=app_config.resolve_runtime_path(
                    config.get(
                        "pubsub_heartbeat_file",
                        DEFAULT_PUBSUB_HEARTBEAT_FILE_RELATIVE,
                    ),
                    base_dir=base_dir,
                ),
                heartbeat_interval_seconds=float(
                    config.get("pubsub_heartbeat_interval_seconds", 30.0)
                ),
                heartbeat_max_age_seconds=float(
                    config.get("pubsub_heartbeat_max_age_seconds", 300.0)
                ),
                trigger_failure_max_consecutive=int(
                    config.get("pubsub_trigger_failure_max_consecutive", 5)
                ),
                trigger_failure_base_delay_seconds=float(
                    config.get("pubsub_trigger_failure_base_delay_seconds", 1.0)
                ),
                trigger_failure_max_delay_seconds=float(
                    config.get("pubsub_trigger_failure_max_delay_seconds", 60.0)
                ),
                stream_reconnect_base_delay_seconds=float(
                    config.get("pubsub_stream_reconnect_base_delay_seconds", 1.0)
                ),
                stream_reconnect_max_delay_seconds=float(
                    config.get("pubsub_stream_reconnect_max_delay_seconds", 60.0)
                ),
                stream_reconnect_max_attempts=int(
                    config.get("pubsub_stream_reconnect_max_attempts", 0)
                ),
                idle_trigger_interval_seconds=float(
                    config.get("pubsub_idle_trigger_interval_seconds", 300.0)
                ),
            ),
            transient_alert=TransientAlertConfig(
                min_duration_seconds=float(
                    config.get("transient_alert_min_duration_seconds", 600.0)
                ),
                cooldown_seconds=float(
                    config.get("transient_alert_cooldown_seconds", 1800.0)
                ),
            ),
            service_status_file=app_config.resolve_runtime_path(
                config.get(
                    "service_status_file",
                    DEFAULT_SERVICE_STATUS_FILE_RELATIVE,
                ),
                base_dir=base_dir,
            ),
            runtime_paths=runtime_paths,
            subject_pattern=compile_optional_pattern(
                config.get("amazon_subject_pattern"), "amazon_subject_pattern"
            ),
        )


def compile_optional_pattern(
    pattern: str | None, config_key: str
) -> Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        app_config.LOGGER.error(
            "CONFIG_INVALID_REGEX: %s=%r error=%s", config_key, pattern, exc
        )
        raise ValueError(
            f"config.json の {config_key} が不正な正規表現です: {exc}"
        ) from exc


def compile_required_pattern(pattern: str, config_key: str) -> Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        app_config.LOGGER.error(
            "CONFIG_INVALID_REGEX: %s=%r error=%s", config_key, pattern, exc
        )
        raise ValueError(
            f"config.json の {config_key} が不正な正規表現です: {exc}"
        ) from exc


def mask_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/...redacted..."


def looks_like_discord_webhook_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme != "https":
        return False
    if parsed.netloc not in {"discord.com", "canary.discord.com", "ptb.discord.com"}:
        return False
    return parsed.path.startswith("/api/webhooks/")


def validate_config(config: dict, *, paths: RuntimePaths | None = None) -> list[str]:
    errors: list[str] = []
    runtime_paths = app_config.get_runtime_paths() if paths is None else paths
    base_dir = runtime_paths.runtime_dir

    webhook = config.get("discord_webhook_url")
    if not isinstance(webhook, str) or not webhook.strip():
        errors.append("discord_webhook_url が未設定です。")

    for key in ("max_messages", "poll_interval_seconds"):
        if key not in config:
            continue
        try:
            value = int(config[key])
        except (TypeError, ValueError):
            errors.append(f"{key} は整数で指定してください。")
            continue
        if value <= 0:
            errors.append(f"{key} は 1 以上を指定してください。")
        if key == "poll_interval_seconds" and value < MIN_POLL_INTERVAL_SECONDS:
            errors.append(
                f"poll_interval_seconds は {MIN_POLL_INTERVAL_SECONDS} 以上を指定してください。"
                f"({value} は短すぎます)"
            )

    retry_keys = (
        "gmail_api_max_retries",
        "discord_max_retries",
        "pubsub_trigger_failure_max_consecutive",
    )
    for key in retry_keys:
        if key not in config:
            continue
        try:
            value = int(config[key])
        except (TypeError, ValueError):
            errors.append(f"{key} は整数で指定してください。")
            continue
        if value < 1:
            errors.append(f"{key} は 1 以上を指定してください。")

    if "pubsub_stream_reconnect_max_attempts" in config:
        try:
            reconnect_attempts = int(config["pubsub_stream_reconnect_max_attempts"])
        except (TypeError, ValueError):
            errors.append(
                "pubsub_stream_reconnect_max_attempts は整数で指定してください。"
            )
        else:
            if reconnect_attempts < 0:
                errors.append(
                    "pubsub_stream_reconnect_max_attempts は 0 以上を指定してください。"
                )

    delay_keys = (
        "gmail_api_base_delay_seconds",
        "gmail_api_max_delay_seconds",
        "discord_base_delay_seconds",
        "discord_max_delay_seconds",
        "pubsub_trigger_failure_base_delay_seconds",
        "pubsub_trigger_failure_max_delay_seconds",
        "pubsub_stream_reconnect_base_delay_seconds",
        "pubsub_stream_reconnect_max_delay_seconds",
    )
    for key in delay_keys:
        if key not in config:
            continue
        try:
            delay_value = float(config[key])
        except (TypeError, ValueError):
            errors.append(f"{key} は数値で指定してください。")
            continue
        if delay_value <= 0:
            errors.append(f"{key} は 0 より大きい値を指定してください。")

    non_negative_delay_keys = (
        "transient_alert_min_duration_seconds",
        "transient_alert_cooldown_seconds",
    )
    for key in non_negative_delay_keys:
        if key not in config:
            continue
        try:
            delay_value = float(config[key])
        except (TypeError, ValueError):
            errors.append(f"{key} は数値で指定してください。")
            continue
        if delay_value < 0:
            errors.append(f"{key} は 0 以上の値を指定してください。")

    if "structured_logging" in config and not isinstance(
        config.get("structured_logging"), bool
    ):
        errors.append("structured_logging は true/false で指定してください。")

    pubsub_subscription = config.get("pubsub_subscription")
    if pubsub_subscription is not None:
        if not isinstance(pubsub_subscription, str) or not pubsub_subscription.strip():
            errors.append(
                "pubsub_subscription は空文字以外の文字列で指定してください。"
            )

    pubsub_main_service_name = config.get("pubsub_main_service_name")
    if pubsub_main_service_name is not None:
        if (
            not isinstance(pubsub_main_service_name, str)
            or not pubsub_main_service_name.strip()
        ):
            errors.append(
                "pubsub_main_service_name は空文字以外の文字列で指定してください。"
            )

    for key in ("pubsub_heartbeat_file", "service_status_file"):
        if key not in config:
            continue
        heartbeat_path_value = config.get(key)
        if (
            not isinstance(heartbeat_path_value, str)
            or not heartbeat_path_value.strip()
        ):
            errors.append(f"{key} は空文字以外の文字列で指定してください。")
            continue
        try:
            app_config.resolve_runtime_path(heartbeat_path_value, base_dir=base_dir)
        except Exception as exc:
            errors.append(f"{key} を runtime パスとして解決できません: {exc}")

    for key in (
        "pubsub_heartbeat_interval_seconds",
        "pubsub_heartbeat_max_age_seconds",
        "pubsub_idle_trigger_interval_seconds",
    ):
        if key not in config:
            continue
        try:
            heartbeat_value = float(config[key])
        except (TypeError, ValueError):
            errors.append(f"{key} は数値で指定してください。")
            continue
        if heartbeat_value <= 0:
            errors.append(f"{key} は 0 より大きい値を指定してください。")

    amazon_from_pattern = config.get("amazon_from_pattern", r"amazon\.co\.jp")
    try:
        re.compile(amazon_from_pattern)
    except re.error as exc:
        errors.append(f"amazon_from_pattern の正規表現が不正です: {exc}")

    subject_pattern = config.get("amazon_subject_pattern")
    if subject_pattern:
        try:
            re.compile(subject_pattern)
        except re.error as exc:
            errors.append(f"amazon_subject_pattern の正規表現が不正です: {exc}")

    for key, default_value in (
        ("state_file", "state.json"),
        ("transient_state_file", DEFAULT_TRANSIENT_STATE_FILE_RELATIVE),
        ("log_file", DEFAULT_LOG_FILE_RELATIVE),
        ("events_file", DEFAULT_EVENTS_FILE_RELATIVE),
        ("runs_file", DEFAULT_RUNS_FILE_RELATIVE),
    ):
        value = config.get(key, default_value)
        if not isinstance(value, str):
            errors.append(f"{key} は空文字以外の文字列で指定してください。")
            continue
        if not value.strip():
            errors.append(f"{key} は空文字以外の文字列で指定してください。")
            continue
        try:
            app_config.resolve_runtime_path(value, base_dir=base_dir)
        except Exception as exc:
            errors.append(f"{key} を runtime パスとして解決できません: {exc}")

    return errors


def build_runtime(
    config: dict,
    *,
    dry_run: bool = False,
    paths: RuntimePaths | None = None,
) -> RuntimeConfig:
    return RuntimeConfig.from_mapping(config, dry_run=dry_run, paths=paths)
