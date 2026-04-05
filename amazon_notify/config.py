import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

RUNTIME_DIR = Path.cwd()
CONFIG_PATH = RUNTIME_DIR / "config.json"
CREDENTIALS_PATH = RUNTIME_DIR / "credentials.json"
TOKEN_PATH = RUNTIME_DIR / "token.json"
DEFAULT_LOG_PATH = RUNTIME_DIR / "logs" / "amazon_mail_notifier.log"


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path
    config: Path
    credentials: Path
    token: Path
    default_log: Path

LOGGER = logging.getLogger("amazon_mail_notifier")


def setup_logging(log_path: Path = DEFAULT_LOG_PATH) -> None:
    """Configure stdout and rotating file logging once."""
    if LOGGER.handlers:
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

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


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"last_message_id": None}
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(path: Path, state: dict) -> None:
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
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def configure_runtime_paths(config_path: str | Path) -> Path:
    global RUNTIME_DIR, CONFIG_PATH, CREDENTIALS_PATH, TOKEN_PATH, DEFAULT_LOG_PATH

    resolved_config_path = Path(config_path).expanduser().resolve()
    runtime_dir = resolved_config_path.parent

    RUNTIME_DIR = runtime_dir
    CONFIG_PATH = resolved_config_path
    CREDENTIALS_PATH = runtime_dir / "credentials.json"
    TOKEN_PATH = runtime_dir / "token.json"
    DEFAULT_LOG_PATH = runtime_dir / "logs" / "amazon_mail_notifier.log"
    return runtime_dir


def get_runtime_paths() -> RuntimePaths:
    return RuntimePaths(
        runtime_dir=RUNTIME_DIR,
        config=CONFIG_PATH,
        credentials=CREDENTIALS_PATH,
        token=TOKEN_PATH,
        default_log=DEFAULT_LOG_PATH,
    )


def resolve_runtime_path(path_value: str | Path, base_dir: Path | None = None) -> Path:
    path = Path(path_value)
    runtime_dir = base_dir if base_dir is not None else RUNTIME_DIR
    return path if path.is_absolute() else runtime_dir / path
