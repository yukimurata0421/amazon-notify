from __future__ import annotations

import json
from pathlib import Path

import amazon_notify.scenario_harness as harness
from amazon_notify.runtime import RuntimeConfig, RuntimePaths


def _runtime(tmp_path: Path) -> RuntimeConfig:
    cfg = {
        "discord_webhook_url": "https://discord.invalid/webhook",
        "state_file": "state.json",
        "events_file": "events.jsonl",
        "runs_file": "runs.jsonl",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    paths = RuntimePaths(
        runtime_dir=tmp_path,
        config=config_path,
        credentials=tmp_path / "credentials.json",
        token=tmp_path / "token.json",
        default_log=tmp_path / "logs" / "amazon_mail_notifier.log",
    )
    return RuntimeConfig.from_mapping(cfg, paths=paths)


def test_run_scenario_harness_with_unknown_scenario(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    code, report = harness.run_scenario_harness(runtime, ["unknown"])
    assert code == 1
    assert report["status"] == "degraded"
    assert report["unknown_scenarios"] == ["unknown"]


def test_run_scenario_harness_handles_unhandled_exception(
    monkeypatch, tmp_path: Path
) -> None:
    runtime = _runtime(tmp_path)

    def boom() -> harness.ScenarioResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(harness, "_scenario_gmail_transient_failure", boom)
    code, report = harness.run_scenario_harness(runtime, ["gmail_transient_failure"])
    assert code == 1
    assert report["status"] == "degraded"
    assert report["results"][0]["ok"] is False
    assert "unhandled" in report["results"][0]["detail"]


def test_scenario_functions_cover_main_paths() -> None:
    assert harness._scenario_gmail_transient_failure().ok is True
    assert harness._scenario_discord_429_retry().ok is True
    assert harness._scenario_discord_timeout_retry().ok is True
    assert harness._scenario_checkpoint_interrupt_window().ok is True
    assert harness._scenario_truncated_jsonl().ok is True
    assert harness._scenario_corrupted_jsonl_middle().ok is True
    assert harness._scenario_enospc_checkpoint().ok is True
    assert harness._scenario_stale_incident_recovery().ok is True


def test_patch_attr_restores_original_value() -> None:
    class Obj:
        value = 1

    obj = Obj()
    with harness._patch_attr(obj, "value", 2):
        assert obj.value == 2
    assert obj.value == 1


def test_resp_default_headers() -> None:
    resp = harness._Resp(204, "ok")
    assert resp.status_code == 204
    assert resp.text == "ok"
    assert resp.headers == {}
