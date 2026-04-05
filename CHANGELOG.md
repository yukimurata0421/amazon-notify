# Changelog

All notable changes to this project will be documented in this file.

## [0.3.0] - 2026-04-05

This release focuses on aligning StreamingPull recovery and checkpoint durability with the frontier-consistency contract.

### Added
- Added Pub/Sub StreamingPull runtime mode (`--streaming-pull`) with realtime trigger processing.
- Added Gmail watch setup command (`--setup-watch --pubsub-topic ...`) with retry handling.
- Added watchdog-based fallback mode (`--fallback-watchdog`) to skip polling when main streaming service is healthy.
- Added heartbeat-driven silent-failure detection for StreamingPull (`--heartbeat-file`, `--heartbeat-interval-seconds`, `--heartbeat-max-age-seconds`).
- Added new deployment templates for hybrid operation:
  - `deployment/systemd/amazon-notify-pubsub.service`
  - `deployment/systemd/amazon-notify-fallback.service`
  - `deployment/systemd/amazon-notify-fallback.timer`

### Changed
- Upgraded Discord Webhook retry behavior to exponential backoff with `429/5xx` awareness and `Retry-After` support.
- Upgraded Gmail API retry behavior to exponential backoff for transient/retryable failures.
- Expanded CLI/config validation to include pubsub/heartbeat/failover settings.
- Reorganized README structure:
  - concise `README.md` (entrypoint)
  - concise Japanese `README.ja.md`
  - detailed architecture article in `docs/HYBRID_ARCHITECTURE_JA.md`.
- Hardened StreamingPull to reduce reliance on `systemd` restarts:
  - replaced queue-drop behavior with latest-event aggregation (no `ack`+drop on local queue full)
  - added trigger failure backoff and consecutive-failure circuit breaker
  - added in-process stream reconnect loop with exponential backoff in CLI runtime.
  - kept `--pubsub-trigger-queue-size` as a backward-compatible alias of `--pubsub-pending-warn-threshold`.
- Refactored CLI mode handlers into smaller units (`handle_setup_watch`, `handle_streaming_pull_mode`, `run_polling_mode`) to reduce `main()` branching complexity.
- Extended heartbeat model to include worker progress timestamps and surfaced worker-stale detection in fallback watchdog health checks.
- Added new runtime config knobs for trigger failure handling and stream reconnect behavior.
- Refined token refresh retry design by allowing `request_factory` injection in `refresh_with_retry` to isolate retry logic from Google dependency initialization.
- Added optional `RuntimePaths` injection to Gmail client auth/service construction paths to reduce direct global-path coupling.
- Compiled `amazon_pattern` once at runtime initialization and unified sender matching to use compiled regex patterns.
- Disabled Gmail discovery cache (`cache_discovery=False`) when building the Google API client to avoid environment-dependent cache warnings.
- Made checkpoint writes align with the source-of-truth contract:
  - write `checkpoint_advanced` event first
  - update `state.json` snapshot as best-effort.
- Tightened JSONL corruption handling:
  - keep tolerating only tail-line corruption
  - fail fast on corrupted middle lines.
- Switched `state.json` persistence to atomic write (`tempfile + os.replace`) to reduce partial-write risk.
- Clarified README guarantees/non-goals and added README language-policy/license notes.
- Updated README release badge link to `releases/latest` and strengthened top-level architecture intent text.
- Aligned README language policy with actual docs language and added a note that `main` may be ahead of the latest GitHub release.
- Moved `engineering-decisions.md` under `docs/` and linked it from both README variants.
- Added `docs/IMPLEMENTATION_RATIONALE_JA.md` to document senior-level decision intent and adoption/non-adoption rationale.
- Added transient-alert boundary controls (`transient_alert_min_duration_seconds`, `transient_alert_cooldown_seconds`) so short self-healing failures do not page operators.
- Added optional JSON structured logging via `structured_logging=true`.
- Added `deployment/systemd/install-systemd.sh` and `make install-systemd` for one-command systemd bootstrap.
- Expanded `deployment/systemd/install-systemd.sh` with explicit environment-boundary options (`--base-dir`, `--system-user`, `--config-path`, `--heartbeat-path`) and automatic unit rendering.
- Updated config validation wording so `poll_interval_seconds` minimum behaves as a required lower bound.
- Standardized operational timestamps to UTC ISO 8601 across pipeline runs, checkpoint events, token/transient markers, and failover state.
- Enabled Ruff import-order lint (`I`) and normalized imports across the project.
- Added operator docs for portability and reproducible hybrid setup:
  - `docs/HYBRID_QUICKSTART_JA.md`
  - `docs/PORTABILITY_PARAMS_JA.md`.
- Hardened persistence-failure handling around JSONL/state writes:
  - explicit `ENOSPC` hinting in checkpoint/run-result write errors
  - failover-safe handling when failure-event persistence itself fails
  - in-memory incident suppression fallback when incident state writes fail.

### Tests
- Expanded unit/e2e tests for new streaming/failover/backoff flows.
- Kept coverage gate (`--cov-fail-under=90`) passing with latest implementation.
- Added CLI reconnect behavior regression test.
- Added watchdog regression test for worker heartbeat stale detection.
- Added transient-alert threshold/cooldown behavior tests and recovery-notification boundary tests.
- Added focused `runtime.py` unit tests and restored CI coverage gate compliance (`--cov-fail-under=90`).

## [0.2.0] - 2026-04-04

### Added
- Introduced domain-driven pipeline building blocks: `MailEnvelope`, `NotificationCandidate`, `Checkpoint`, `AuthStatus`, and `RunResult`.
- Added protocol boundaries for `MailSource`, `Notifier`, `Classifier`, and `CheckpointStore`.
- Added append-only operational records:
  - `events.jsonl` (`checkpoint_advanced`, `delivery_failed`, `message_detail_failed`, `source_failed`, `auth_failed`)
  - `runs.jsonl` structured run summaries with checkpoint before/after and counters.
- Added incident lifecycle events (`incident_opened`, `incident_suppressed`, `incident_recovered`) to suppress repetitive alert spam.
- Added migration bootstrap from legacy `state.json` to `events.jsonl` on first run.
- Added contract tests for critical guarantees:
  - checkpoint advances only on successful delivery path
  - delivery/detail failures do not advance checkpoint
  - auth failure is recorded as `auth_failed`.

### Changed
- Refactored `run_once` into a transaction-like notification pipeline with explicit commit semantics.
- Decoupled Gmail/Discord integrations from core flow via adapter classes.
- Reworked auth handling to expose explicit `AuthStatus` transitions (missing/corrupted/refresh/build failure/ready).
- Upgraded config validation with semantic checks:
  - `poll_interval_seconds` now enforces a practical minimum
  - runtime path validation includes `events_file` and `runs_file`.
- Hardened JSONL durability:
  - per-record `flush` + `fsync`
  - `schema_version` fields
  - ignore corrupted tail line on startup recovery.

## [0.1.3] - 2026-04-03

### Changed
- Added Ruff linting and mypy type checking to the CI pipeline.
- Improved retry/error-handling internals (`refresh_with_retry`, transient error chain depth guard).
- Improved notifier observability by adding non-Amazon skip counts to completion logs.
- Tightened type hints around runtime state file handling.

## [0.1.2] - 2026-04-03

### Added
- Added CLI operational commands: `--dry-run`, `--test-discord`, `--validate-config`, and `--health-check` (JSON output).
- Added test coverage execution via `pytest-cov`, `make coverage`, and CI coverage reporting.

### Changed
- Improved automated test coverage to 80%+ (current total: 88% with `pytest --cov`).

## [0.1.1] - 2026-04-02

### Changed
- Unified the public name to `amazon-notify` across the README, systemd sample, package metadata, and distribution archive.
- Renamed the distribution archive to `dist/amazon-notify.zip`.
- Added a notification example and tighter release-facing documentation.

## [0.1.0] - 2026-04-02

### Added
- Initial public release of the Gmail-to-Discord Amazon mail notifier.
- Gmail polling, Amazon mail filtering, Discord notifications, and state-based duplicate suppression.
- Token error alerts, transient failure recovery notifications, tests, CI, and systemd deployment sample.
