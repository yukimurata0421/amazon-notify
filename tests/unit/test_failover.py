import json
from pathlib import Path

from amazon_notify import failover


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_evaluate_main_health_reports_healthy_when_service_active_and_heartbeat_fresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(failover, "get_systemd_service_state", lambda _name: "active")
    monkeypatch.setattr(failover.time, "time", lambda: heartbeat_file.stat().st_mtime + 10)

    status = failover.evaluate_main_health(
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert status.healthy is True
    assert status.heartbeat_age_seconds == 10


def test_get_systemd_service_state_handles_oserror(monkeypatch) -> None:
    monkeypatch.setattr(
        failover.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing systemctl")),
    )
    assert failover.get_systemd_service_state("amazon-notify-pubsub.service") is None


def test_get_systemd_service_state_returns_non_active_status(monkeypatch) -> None:
    class _Proc:
        returncode = 3
        stdout = "failed\n"
        stderr = ""

    monkeypatch.setattr(failover.subprocess, "run", lambda *_args, **_kwargs: _Proc())
    assert failover.get_systemd_service_state("amazon-notify-pubsub.service") == "failed"


def test_evaluate_main_health_reports_missing_heartbeat(monkeypatch, tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"
    monkeypatch.setattr(failover, "get_systemd_service_state", lambda _name: "active")

    status = failover.evaluate_main_health(
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert status.healthy is False
    assert "heartbeat_missing" in status.reason


def test_evaluate_main_health_reports_unhealthy_when_service_inactive(monkeypatch, tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(failover, "get_systemd_service_state", lambda _name: "failed")

    status = failover.evaluate_main_health(
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert status.healthy is False
    assert "service_not_active" in status.reason


def test_evaluate_main_health_reports_stale_heartbeat(monkeypatch, tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(failover, "get_systemd_service_state", lambda _name: "active")
    monkeypatch.setattr(failover.time, "time", lambda: heartbeat_file.stat().st_mtime + 500)

    status = failover.evaluate_main_health(
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert status.healthy is False
    assert "heartbeat_stale" in status.reason


def test_evaluate_main_health_reports_worker_stale_when_snapshot_worker_old(
    monkeypatch,
    tmp_path: Path,
) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": 1_000.0,
                "worker_last_seen_at": 800.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(failover, "get_systemd_service_state", lambda _name: "active")
    monkeypatch.setattr(failover.time, "time", lambda: 1_100.0)

    status = failover.evaluate_main_health(
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert status.healthy is False
    assert "worker_heartbeat_stale" in status.reason


def test_evaluate_main_health_when_service_state_unknown_and_heartbeat_fresh(monkeypatch, tmp_path: Path) -> None:
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(failover, "get_systemd_service_state", lambda _name: None)
    monkeypatch.setattr(failover.time, "time", lambda: heartbeat_file.stat().st_mtime + 5)

    status = failover.evaluate_main_health(
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert status.healthy is True
    assert "service_state_unknown_but_heartbeat_fresh" in status.reason


def test_evaluate_failover_watchdog_switches_to_failover_and_then_recovers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "m-1"}), encoding="utf-8")
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")

    alerts: list[str] = []
    recoveries: list[str] = []
    monkeypatch.setattr(failover, "send_discord_alert", lambda _w, message: alerts.append(message) or True)
    monkeypatch.setattr(failover, "send_discord_recovery", lambda _w, message: recoveries.append(message) or True)

    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=False,
            reason="heartbeat_stale",
            service_state="active",
            heartbeat_age_seconds=999.0,
        ),
    )
    should_run = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert should_run is True
    saved = _read_json(state_file)
    assert saved["pubsub_failover_active"] is True
    assert len(alerts) == 1

    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=True,
            reason="main_healthy",
            service_state="active",
            heartbeat_age_seconds=5.0,
        ),
    )
    should_run_after_recovery = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert should_run_after_recovery is False
    saved_after = _read_json(state_file)
    assert "pubsub_failover_active" not in saved_after
    assert len(recoveries) == 1


def test_evaluate_failover_watchdog_passes_dedupe_state_path_to_alert_and_recovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_message_id": "m-1"}), encoding="utf-8")
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")

    alert_paths: list[Path | None] = []
    recovery_paths: list[Path | None] = []

    def fake_send_alert(_webhook_url: str, _message: str, **kwargs) -> bool:
        alert_paths.append(kwargs.get("dedupe_state_path"))
        return True

    def fake_send_recovery(_webhook_url: str, _message: str, **kwargs) -> bool:
        recovery_paths.append(kwargs.get("dedupe_state_path"))
        return True

    monkeypatch.setattr(failover, "send_discord_alert", fake_send_alert)
    monkeypatch.setattr(failover, "send_discord_recovery", fake_send_recovery)

    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=False,
            reason="heartbeat_stale",
            service_state="active",
            heartbeat_age_seconds=999.0,
        ),
    )
    should_run = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert should_run is True

    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=True,
            reason="main_healthy",
            service_state="active",
            heartbeat_age_seconds=5.0,
        ),
    )
    should_run_after_recovery = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert should_run_after_recovery is False

    expected = state_file.parent / ".discord_dedupe_state.json"
    assert alert_paths == [expected]
    assert recovery_paths == [expected]


def test_evaluate_failover_watchdog_recovery_alert_failure_keeps_active_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "last_message_id": "m-1",
                "pubsub_failover_active": True,
                "pubsub_failover_reason": "stale",
                "pubsub_failover_suppressed_count": 2,
            }
        ),
        encoding="utf-8",
    )
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=True,
            reason="main_healthy",
            service_state="active",
            heartbeat_age_seconds=4.0,
        ),
    )
    monkeypatch.setattr(failover, "send_discord_recovery", lambda *_args, **_kwargs: False)

    should_run = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert should_run is False
    saved = _read_json(state_file)
    assert saved["pubsub_failover_active"] is True


def test_evaluate_failover_watchdog_dry_run_recovery_path(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "last_message_id": "m-1",
                "pubsub_failover_active": True,
                "pubsub_failover_reason": "stale",
                "pubsub_failover_suppressed_count": 2,
            }
        ),
        encoding="utf-8",
    )
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=True,
            reason="main_healthy",
            service_state="active",
            heartbeat_age_seconds=4.0,
        ),
    )

    should_run = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
        dry_run=True,
    )
    assert should_run is False


def test_evaluate_failover_watchdog_suppresses_repeated_failover(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "last_message_id": "m-1",
                "pubsub_failover_active": True,
                "pubsub_failover_reason": "stale",
                "pubsub_failover_suppressed_count": 2,
            }
        ),
        encoding="utf-8",
    )
    heartbeat_file = tmp_path / "heartbeat.txt"
    heartbeat_file.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(
        failover,
        "evaluate_main_health",
        lambda **_kwargs: failover.MainHealthStatus(
            healthy=False,
            reason="heartbeat_stale",
            service_state="active",
            heartbeat_age_seconds=999.0,
        ),
    )

    should_run = failover.evaluate_failover_watchdog(
        state_file=state_file,
        discord_webhook_url="https://discord.invalid/webhook",
        service_name="amazon-notify-pubsub.service",
        heartbeat_file=heartbeat_file,
        heartbeat_max_age_seconds=120,
    )
    assert should_run is True
    saved = _read_json(state_file)
    assert saved["pubsub_failover_suppressed_count"] == 3
