from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from amazon_notify import cli
from amazon_notify.runtime import RuntimeConfig
from amazon_notify.scenarios import list_scenarios, run_scenarios


def _config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "amazon_from_pattern": r"amazon\.co\.jp",
            }
        ),
        encoding="utf-8",
    )


def test_scenario_registry_has_expected_defaults() -> None:
    names = list_scenarios()
    assert "truncated_jsonl" in names
    assert "corrupted_jsonl_middle" in names
    assert "enospc_checkpoint" in names


def test_run_single_scenario(tmp_path: Path) -> None:
    runtime = RuntimeConfig.from_mapping(
        {
            "discord_webhook_url": "https://discord.invalid/webhook",
            "amazon_from_pattern": r"amazon\.co\.jp",
        }
    )

    results = run_scenarios(runtime, ["truncated_jsonl"])
    assert len(results) == 1
    assert results[0].name == "truncated_jsonl"
    assert results[0].ok is True


def test_run_default_scenarios_and_unknown_name(tmp_path: Path) -> None:
    runtime = RuntimeConfig.from_mapping(
        {
            "discord_webhook_url": "https://discord.invalid/webhook",
            "amazon_from_pattern": r"amazon\.co\.jp",
        }
    )

    results = run_scenarios(runtime)
    by_name = {result.name: result for result in results}

    assert set(by_name) == set(list_scenarios())
    assert all(result.ok for result in results)
    assert by_name["checkpoint_interrupt_window"].details["checkpoint_index_rebuilt"]
    assert by_name["corrupted_jsonl_middle"].details["events_readable"] is False
    assert "ENOSPC" in by_name["enospc_checkpoint"].details["error"]
    assert (
        "active incident"
        in by_name["stale_incident_recovery"].details["incident_detail"]
    )

    missing = run_scenarios(runtime, ["missing_scenario"])
    assert missing[0].ok is False
    assert missing[0].details == {"error": "unknown scenario: missing_scenario"}


def test_cli_scenario_harness_exits_zero(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    _config(config_path)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "amazon-notify",
            "--config",
            str(config_path),
            "--scenario-harness",
            "--scenario-names",
            "truncated_jsonl",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 0
    raw = capsys.readouterr().out
    out = json.loads(raw[raw.find("{") :])
    assert out["status"] == "ok"
    assert out["results"][0]["name"] == "truncated_jsonl"
