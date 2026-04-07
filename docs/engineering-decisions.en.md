# Engineering Decisions (English)

This document explains why `amazon-notify` uses its current architecture and operational behaviors.
Target: `main` branch implementation.
For release-focused change intent, see `docs/IMPLEMENTATION_RATIONALE.en.md`.

## 1. Product Constraints
- Single-operator, self-hosted notification tool.
- Gmail -> Discord for Amazon.co.jp delivery-related emails.
- Primary priority is ordered-frontier consistency, not max throughput.
- Single-process / single-host as the default operating model.

## 2. Why Pipeline + Domain Boundaries
Adopted:
- `NotificationPipeline` (`pipeline.py`)
- Domain types (`domain.py`): `MailEnvelope`, `NotificationCandidate`, `Checkpoint`, `AuthStatus`, `RunResult`
- Protocol boundaries: `MailSource`, `Classifier`, `Notifier`, `CheckpointStore`

Reasoning:
- Keep processing invariants (when to advance checkpoint) independent from Gmail/Discord implementation details.
- Make failure-policy behavior explicit and testable.
- Reduce future replacement cost (new source/target integrations).

## 3. Why `events.jsonl` Is the Checkpoint Source of Truth
Adopted:
- Source of truth: `events.jsonl` (`checkpoint_advanced` events)
- Derived artifacts: `state.json` compatibility snapshot, `runs.jsonl` audit summary

Reasoning:
- One authoritative checkpoint history avoids silent divergence.
- Append-only records improve auditability and debugging.
- `state.json` remains for backward compatibility.

Write order:
1. append `checkpoint_advanced` to `events.jsonl`
2. best-effort update `state.json`

This prevents the dangerous case where only derived state advances.

Runtime artifact boundary:
- `events.jsonl`: authoritative checkpoint source.
- `state.json` / `runs.jsonl`: derived compatibility/audit views.
- `*.index.json`: rebuildable caches, not authoritative state.
- `.discord_dedupe_state.json` + lock files: notification dedupe coordination state.

## 4. Why Ordered Frontier (Stop on Midstream Failure)
Policy:
- Process oldest-first.
- Stop run on `message_detail_failed` or `delivery_failed`.
- Advance checkpoint only to the last confirmed success.

Reasoning:
- Avoid checkpoint holes and non-reproducible state.
- Prefer consistency and recoverability over partial forward progress.

## 5. Why Policy-Oriented Error Classes
Adopted (`errors.py`):
- `TransientSourceError`, `PermanentAuthError`, `MessageDecodeError`, `DeliveryError`, `CheckpointError`, `ConfigError`

Reasoning:
- Decisions are made by policy dimensions (`retry?`, `alert?`, `checkpoint?`) rather than transport-specific exception names.
- Operational intent is easier to read and maintain.

## 6. Why Auth Is Modeled as `AuthStatus`
Adopted:
- Explicit auth state enum and transitions.

Reasoning:
- Consistent behavior across CLI, run summaries, and health checks.
- Enables clean integration with incident suppression and recovery notifications.

## 7. Why Incident Lifecycle Exists
Adopted events:
- `incident_opened`, `incident_suppressed`, `incident_recovered`
- transient alert boundary controls:
  - `transient_alert_min_duration_seconds`
  - `transient_alert_cooldown_seconds`

Reasoning:
- Reduce alert fatigue for repeated same-kind failures.
- Preserve visibility of open/recovered states.
- Avoid paging for short self-healing glitches.

## 8. Why JSONL Durability Is Strengthened
Adopted:
- per-record `flush + fsync`
- `schema_version` in records
- tolerate only tail-line corruption
- fail-fast on middle-line corruption
- atomic `state.json` writes (`tempfile + os.replace`)

Reasoning:
- Improve low-cost durability for edge/self-hosted environments.
- Preserve strong checkpoint interpretation guarantees.

## 9. Additional Decision: Disk Full (`ENOSPC`) Handling
Adopted:
- Detect `ENOSPC` explicitly and include a clear disk-capacity hint in persistence errors.
- Keep failure-event persistence as best-effort when already handling an error.
- If run-result persistence fails, convert the run outcome to `checkpoint_failed` so alert flow still works.
- Add in-memory incident suppression fallback when incident state writes fail.

Reasoning:
- "Fail safely" is not enough; investigation also needs fast root-cause discovery.
- Persistence failures must not break the alerting/control path itself.
- When state files are unwritable, pure state-based suppression can no longer prevent repeated alerts.

## 10. External Host Signals (Current Limitation)
Current implementation does not pull remote host metrics directly.
Design decisions here therefore rely on local first-party signals (`OSError`, app logs).
If external metrics become available (disk usage, inode alerts), they should be used as primary corroborating evidence.

## 11. Why Config Validation Includes Semantic Checks
Adopted validation includes:
- lower bounds (for example minimum poll interval)
- runtime-path resolvability
- required operational keys

Reasoning:
- Catch operational misconfiguration before runtime failures.

## 12. Why We Did Not Add Heavier Infrastructure
Not adopted:
- DB/ORM
- distributed queue/workflow engine
- multi-instance coordination
- excessive plugin decomposition

Reasoning:
- Out of scope for single-host reliability target.
- JSONL + ordered frontier currently provides better cost/performance/operability balance.

## 13. Why We Added Rebuildable JSONL Index Snapshots
Adopted:
- `events.jsonl.checkpoint.index.json`
- `runs.jsonl.summary.index.json`

Reasoning:
- Prevent startup/runtime-status reads from growing linearly with long-lived JSONL files.
- Keep source-of-truth as append-only JSONL while allowing fast warm reads.
- Preserve recoverability: indexes are derivable caches, not authoritative state.

## 14. Why Guard-Path Exceptions Converge to `RunResult`
Adopted:
- `run_once_with_guard` unhandled errors are recorded through `report_unhandled_exception` as persisted `source_failed` + `RunResult`.

Reasoning:
- Avoid split operational policies between "normal failure path" and "unhandled path".
- Keep incident lifecycle, summaries, and alerting on a single contract surface.

## 15. Why Incident Memory Suppression Moved off Module Globals
Adopted:
- in-memory suppression map is runtime-scoped (`RuntimeConfig`) instead of mutable module-global state.

Reasoning:
- Improve test isolation and future multi-runtime safety.
- Reduce hidden shared state and fixture-only cleanup dependency.

## 16. Why Discord Dedupe Lock Became Fail-Fast
Adopted:
- require `fcntl` for dedupe file-lock path; unsupported platforms fail fast and surface via health-check (`dedupe_lock_supported`).

Reasoning:
- Silent lock degradation can create hard-to-debug duplicate notification behavior.
- Explicit incompatibility signaling is safer for operations than best-effort locking on unsupported platforms.
