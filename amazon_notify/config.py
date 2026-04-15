from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_DIR = Path.cwd()
DEFAULT_CONFIG_PATH = DEFAULT_RUNTIME_DIR / "config.json"
DEFAULT_CREDENTIALS_PATH = DEFAULT_RUNTIME_DIR / "credentials.json"
DEFAULT_TOKEN_PATH = DEFAULT_RUNTIME_DIR / "token.json"
DEFAULT_LOG_PATH = DEFAULT_RUNTIME_DIR / "logs" / "amazon_mail_notifier.log"


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path
    config: Path
    credentials: Path
    token: Path
    default_log: Path


LOGGER = logging.getLogger("amazon_mail_notifier")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
        }
        if record.exc_info:
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_path: Path | None = None, *, structured: bool = False) -> None:
    """Configure stdout and rotating file logging once."""
    if LOGGER.handlers:
        return

    if log_path is None:
        log_path = DEFAULT_LOG_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    formatter = (
        JsonLogFormatter()
        if structured
        else logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    LOGGER.propagate = False


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        raw: dict[str, Any] = json.load(file)
        return raw


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_message_id": None}
    with path.open("r", encoding="utf-8") as file:
        raw: dict[str, Any] = json.load(file)
        return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def get_runtime_paths(config_path: str | Path | None = None) -> RuntimePaths:
    if config_path is None:
        resolved_config_path = DEFAULT_CONFIG_PATH.resolve()
    else:
        resolved_config_path = Path(config_path).expanduser().resolve()
    runtime_dir = resolved_config_path.parent
    return RuntimePaths(
        runtime_dir=runtime_dir,
        config=resolved_config_path,
        credentials=runtime_dir / "credentials.json",
        token=runtime_dir / "token.json",
        default_log=runtime_dir / "logs" / "amazon_mail_notifier.log",
    )


def resolve_runtime_path(path_value: str | Path, base_dir: Path | None = None) -> Path:
    path = Path(path_value)
    runtime_dir = base_dir if base_dir is not None else DEFAULT_RUNTIME_DIR
    return path if path.is_absolute() else runtime_dir / path
