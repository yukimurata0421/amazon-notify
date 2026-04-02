import json
from pathlib import Path

from amazon_notify import config


def _clear_logger_handlers() -> None:
    for handler in list(config.LOGGER.handlers):
        config.LOGGER.removeHandler(handler)
        handler.close()


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    original_handlers = list(config.LOGGER.handlers)
    original_propagate = config.LOGGER.propagate
    original_level = config.LOGGER.level

    try:
        _clear_logger_handlers()
        log_path = tmp_path / "logs" / "notifier.log"

        config.setup_logging(log_path)
        assert len(config.LOGGER.handlers) == 2
        assert config.LOGGER.propagate is False
        assert log_path.exists()

        config.setup_logging(log_path)
        assert len(config.LOGGER.handlers) == 2
    finally:
        _clear_logger_handlers()
        for handler in original_handlers:
            config.LOGGER.addHandler(handler)
        config.LOGGER.propagate = original_propagate
        config.LOGGER.setLevel(original_level)


def test_load_config_and_state_helpers(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"discord_webhook_url": "x"}), encoding="utf-8")
    assert config.load_config(config_path)["discord_webhook_url"] == "x"

    state_path = tmp_path / "state.json"
    assert config.load_state(state_path) == {"last_message_id": None}

    config.save_state(state_path, {"last_message_id": "m-1"})
    assert config.load_state(state_path)["last_message_id"] == "m-1"


def test_resolve_runtime_path_uses_base_dir_for_relative_path(tmp_path: Path) -> None:
    assert config.resolve_runtime_path("state.json", base_dir=tmp_path) == tmp_path / "state.json"
    abs_path = tmp_path / "absolute.json"
    assert config.resolve_runtime_path(abs_path, base_dir=tmp_path) == abs_path
