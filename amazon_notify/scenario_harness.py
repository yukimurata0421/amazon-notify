from __future__ import annotations

import errno
import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import requests

from .checkpoint_store import JsonlCheckpointStore
from .discord_client import _SESSION, _post_webhook
from .domain import Checkpoint, FailureKind
from .errors import CheckpointError, TransientSourceError
from .pipeline import NotificationPipeline
from .runtime import RuntimeConfig
from .status import build_doctor_report
from .verify_state import run_verify_state


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str


def run_scenario_harness(
    runtime: RuntimeConfig, scenario_names: list[str] | None = None
) -> tuple[int, dict[str, Any]]:
    available: dict[str, Callable[[], ScenarioResult]] = {
        "gmail_transient_failure": _scenario_gmail_transient_failure,
        "discord_429_retry": _scenario_discord_429_retry,
        "discord_timeout_retry": _scenario_discord_timeout_retry,
        "checkpoint_interrupt_window": _scenario_checkpoint_interrupt_window,
        "truncated_jsonl": _scenario_truncated_jsonl,
        "corrupted_jsonl_middle": _scenario_corrupted_jsonl_middle,
        "read_only_state": _scenario_read_only_state,
        "enospc_checkpoint": _scenario_enospc_checkpoint,
        "stale_incident_recovery": _scenario_stale_incident_recovery,
    }

    selected = scenario_names or list(available.keys())
    results: list[ScenarioResult] = []
    unknown = [name for name in selected if name not in available]
    for name in selected:
        if name not in available:
            continue
        try:
            results.append(available[name]())
        except Exception as exc:  # pragma: no cover - harness hardening
            results.append(
                ScenarioResult(name=name, ok=False, detail=f"unhandled: {exc}")
            )

    ok = not unknown and all(item.ok for item in results)
    report = {
        "status": "ok" if ok else "degraded",
        "requested": selected,
        "unknown_scenarios": unknown,
        "results": [
            {
                "name": item.name,
                "ok": item.ok,
                "detail": item.detail,
            }
            for item in results
        ],
        "runtime_context": {
            "state_file": str(runtime.state_file),
            "events_file": str(runtime.events_file),
            "runs_file": str(runtime.runs_file),
        },
    }
    return (0 if ok else 1), report


class _DummySource:
    def __init__(self):
        self.transient_marked = False

    def get_auth_status(self):
        return None

    def notify_recovery_if_needed(self) -> None:
        return None

    def mark_transient_issue(self, _err: Exception | str) -> None:
        self.transient_marked = True

    def iter_new_messages(self, _checkpoint: Checkpoint, _max_messages: int):
        raise TransientSourceError("transient gmail source failure")


class _DummyClassifier:
    def classify(self, _envelope):
        return None


class _DummyNotifier:
    def notify(self, _candidate) -> bool:
        return True


class _DummyCheckpointStore:
    def __init__(self):
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def load_checkpoint(self) -> Checkpoint:
        return Checkpoint(message_id="cp-0")

    def advance_checkpoint(self, checkpoint: Checkpoint, run_id: str) -> None:
        self.events.append(
            ("checkpoint_advanced", run_id, {"checkpoint": checkpoint.message_id})
        )

    def append_event(
        self, event_type: str, run_id: str, payload: dict[str, Any]
    ) -> None:
        self.events.append((event_type, run_id, payload))

    def append_run_result(self, _result) -> None:
        return None


def _scenario_gmail_transient_failure() -> ScenarioResult:
    source = _DummySource()
    store = _DummyCheckpointStore()
    pipeline = NotificationPipeline(
        source=source,
        classifier=_DummyClassifier(),
        notifier=_DummyNotifier(),
        checkpoint_store=store,
        max_messages=10,
        dry_run=True,
    )
    result = pipeline.run_once()
    ok = (
        result.failure_kind == FailureKind.SOURCE_FAILED
        and result.should_retry is True
        and result.should_alert is False
        and source.transient_marked is True
    )
    detail = (
        "transient source failure is persisted and marked"
        if ok
        else "unexpected pipeline behavior"
    )
    return ScenarioResult(name="gmail_transient_failure", ok=ok, detail=detail)


@contextmanager
def _patch_attr(obj: object, attr: str, replacement: object):
    original = getattr(obj, attr)
    setattr(obj, attr, replacement)
    try:
        yield
    finally:
        setattr(obj, attr, original)


class _Resp:
    def __init__(
        self, status_code: int, text: str = "", headers: dict[str, str] | None = None
    ):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _scenario_discord_429_retry() -> ScenarioResult:
    calls = {"count": 0}
    responses = [_Resp(429, "rate limit", {"Retry-After": "0"}), _Resp(204, "ok")]

    def fake_post(*_args, **_kwargs):
        idx = calls["count"]
        calls["count"] += 1
        return responses[min(idx, len(responses) - 1)]

    with (
        _patch_attr(_SESSION, "post", fake_post),
        _patch_attr(time, "sleep", lambda _sec: None),
    ):
        ok = _post_webhook("https://discord.invalid/webhook", "hello", max_attempts=3)

    success = ok and calls["count"] >= 2
    detail = "429 retry path recovered" if success else "429 retry path failed"
    return ScenarioResult(name="discord_429_retry", ok=success, detail=detail)


def _scenario_discord_timeout_retry() -> ScenarioResult:
    calls = {"count": 0}

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.exceptions.Timeout("timeout")
        return _Resp(204, "ok")

    with (
        _patch_attr(_SESSION, "post", fake_post),
        _patch_attr(time, "sleep", lambda _sec: None),
    ):
        ok = _post_webhook("https://discord.invalid/webhook", "hello", max_attempts=3)

    success = ok and calls["count"] >= 2
    detail = "timeout retry path recovered" if success else "timeout retry path failed"
    return ScenarioResult(name="discord_timeout_retry", ok=success, detail=detail)


def _scenario_checkpoint_interrupt_window() -> ScenarioResult:
    with TemporaryDirectory(prefix="scenario-checkpoint-") as d:
        root = Path(d)
        (root / "events.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "checkpoint_advanced",
                    "run_id": "run-1",
                    "at": "2026-04-10T00:00:00+00:00",
                    "checkpoint": "cp-new",
                    "source": "pipeline_commit",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "state.json").write_text(
            json.dumps({"last_message_id": "cp-old"}), encoding="utf-8"
        )
        runtime = RuntimeConfig.from_mapping(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "runs_file": str(root / "runs.jsonl"),
            },
            dry_run=True,
        )
        code, report = build_doctor_report(runtime)
    mismatch = any(
        check.get("name") == "checkpoint_state_consistent" and not bool(check.get("ok"))
        for check in report.get("checks", [])
    )
    ok = code == 1 and mismatch
    return ScenarioResult(
        name="checkpoint_interrupt_window",
        ok=ok,
        detail="checkpoint/state divergence detected"
        if ok
        else "divergence not detected",
    )


def _scenario_truncated_jsonl() -> ScenarioResult:
    with TemporaryDirectory(prefix="scenario-truncated-") as d:
        root = Path(d)
        (root / "events.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "checkpoint_advanced",
                    "run_id": "run-1",
                    "at": "2026-04-10T00:00:00+00:00",
                    "checkpoint": "cp-1",
                    "source": "pipeline_commit",
                }
            )
            + '\n{"broken":',
            encoding="utf-8",
        )
        (root / "state.json").write_text(
            json.dumps({"last_message_id": "cp-1"}), encoding="utf-8"
        )
        runtime = RuntimeConfig.from_mapping(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "runs_file": str(root / "runs.jsonl"),
            },
            dry_run=True,
        )
        code, report = build_doctor_report(runtime)

    # tail corruption is ignored by design
    ok = (
        code == 0
        and report.get("runtime_status", {})
        .get("tail_corruption_ignored", {})
        .get("events_jsonl")
        is True
    )
    return ScenarioResult(
        name="truncated_jsonl",
        ok=ok,
        detail="tail truncation tolerated" if ok else "tail truncation handling failed",
    )


def _scenario_corrupted_jsonl_middle() -> ScenarioResult:
    with TemporaryDirectory(prefix="scenario-corrupt-") as d:
        root = Path(d)
        (root / "runs.jsonl").write_text(
            '{"schema_version":1,"run_id":"ok"}\n{"broken":\n{"schema_version":1,"run_id":"ok2"}\n',
            encoding="utf-8",
        )
        runtime = RuntimeConfig.from_mapping(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "runs_file": str(root / "runs.jsonl"),
            },
            dry_run=True,
        )
        code, report = build_doctor_report(runtime)

    corrupted = any(
        check.get("name") == "runs_jsonl_readable" and not bool(check.get("ok"))
        for check in report.get("checks", [])
    )
    ok = code == 1 and corrupted
    return ScenarioResult(
        name="corrupted_jsonl_middle",
        ok=ok,
        detail="middle corruption detected" if ok else "middle corruption not detected",
    )


def _scenario_read_only_state() -> ScenarioResult:
    with TemporaryDirectory(prefix="scenario-ro-") as d:
        root = Path(d)
        events = root / "events.jsonl"
        state = root / "state.json"
        state.write_text(json.dumps({"last_message_id": "cp-0"}), encoding="utf-8")
        store = JsonlCheckpointStore(
            state_file=state, events_file=events, runs_file=root / "runs.jsonl"
        )

        root.chmod(0o500)
        try:
            try:
                store.advance_checkpoint(Checkpoint(message_id="cp-1"), "run-ro")
                ok = False
            except CheckpointError:
                ok = True
        finally:
            root.chmod(0o700)

    return ScenarioResult(
        name="read_only_state",
        ok=ok,
        detail="read-only write failure surfaced"
        if ok
        else "read-only failure not surfaced",
    )


def _scenario_enospc_checkpoint() -> ScenarioResult:
    with TemporaryDirectory(prefix="scenario-enospc-") as d:
        root = Path(d)
        store = JsonlCheckpointStore(
            state_file=root / "state.json",
            events_file=root / "events.jsonl",
            runs_file=root / "runs.jsonl",
        )

        original = store.append_event

        def fail_append(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        store.append_event = fail_append  # type: ignore[method-assign]
        try:
            try:
                store.advance_checkpoint(Checkpoint(message_id="cp-1"), "run-enospc")
                ok = False
            except CheckpointError as exc:
                ok = "ENOSPC" in str(exc)
        finally:
            store.append_event = original  # type: ignore[method-assign]

    return ScenarioResult(
        name="enospc_checkpoint",
        ok=ok,
        detail="ENOSPC hint propagated" if ok else "ENOSPC hint missing",
    )


def _scenario_stale_incident_recovery() -> ScenarioResult:
    with TemporaryDirectory(prefix="scenario-incident-") as d:
        root = Path(d)
        (root / "events.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event": "incident_recovered",
                    "run_id": "run-2",
                    "at": "2026-04-10T01:00:00+00:00",
                    "kind": "delivery_failed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "state.json").write_text(
            json.dumps(
                {
                    "last_message_id": "cp-1",
                    "active_incident_kind": "delivery_failed",
                    "active_incident_at": "2026-04-10T00:00:00+00:00",
                    "incident_suppressed_count": 1,
                }
            ),
            encoding="utf-8",
        )
        runtime = RuntimeConfig.from_mapping(
            {
                "discord_webhook_url": "https://discord.invalid/webhook",
                "state_file": str(root / "state.json"),
                "events_file": str(root / "events.jsonl"),
                "runs_file": str(root / "runs.jsonl"),
            },
            dry_run=True,
        )
        code, report = run_verify_state(runtime)

    stale = any(
        check.get("name") == "incident_lifecycle_consistent"
        and not bool(check.get("ok"))
        for check in report.get("checks", [])
    )
    ok = code == 1 and stale
    return ScenarioResult(
        name="stale_incident_recovery",
        ok=ok,
        detail="stale incident state detected"
        if ok
        else "stale incident state not detected",
    )
