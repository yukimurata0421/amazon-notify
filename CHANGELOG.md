# Changelog

All notable changes to this project will be documented in this file.

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
