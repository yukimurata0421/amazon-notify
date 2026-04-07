# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

Summary:
- Runtime state handling was reorganized around explicit path injection, incident-state storage separation, and index rebuild tooling.
- StreamingPull internals were split for maintainability while preserving existing runtime behavior and health/heartbeat outputs.
- Documentation was expanded in Japanese/English, including hybrid setup, portability notes, and a thin Docker evaluation path.
- Discord dedupe path resolution was aligned with runtime path injection, and Gmail runtime logic was split into auth/transient-state modules while preserving the public API surface.

### Changed
- Added a global Discord notification dedupe layer (alert/recovery/test/delivery) with idempotency keys, cross-process lock coordination, and in-flight claim handling to suppress duplicate sends under concurrent runtimes.
- Set the next intended release cut to `0.4.0` to group runtime-state model upgrades (checkpoint/run-summary indices, guard-path normalization, and incident-memory refactor) into one semantically-visible update.
- Added lock/state runtime artifact ignores for dedupe coordination files (`.state.json.lock`, `.discord_dedupe_state.json`, `.discord_dedupe_state.lock`).
- Hardened Discord dedupe-state parsing/pruning to explicitly drop malformed inflight entries (no dangling owner-only entries).
- Added paginated Gmail listing for polling catch-up and fail-safe behavior when the checkpoint is not found in listing results, so checkpoint advancement never skips unseen frontier messages under backlog pressure.
- Bound loop lambdas in `GmailMailSource.iter_new_messages` with default arguments to avoid future closure-capture pitfalls.
- Switched `record_transient_issue` negative threshold handling from exception-fail to warning+clamp (`<0` -> `0`) for defensive resilience against bad runtime values.
- Documented silent-clear behavior in operations docs (no recovery notification when transient alert threshold was never crossed).
- Clarified README platform assumptions (`Linux`/single-host/systemd-first) and OAuth-browser completion step in quickstart.
- Expanded English/Japanese operations/design docs to cover runtime index snapshots, guard-path `RunResult` convergence, dedupe lock fail-fast semantics, and health-check lock support signaling.
- Added a minimal Docker runtime baseline (`Dockerfile`, `.dockerignore`, and English/Japanese Docker guides) focused on CLI bring-up (`--help`, `--validate-config`, `--once --dry-run`) without systemd/hybrid/watchdog orchestration scope.
- Clarified Docker positioning as a supplemental quick-evaluation path (portability/reproducibility aid), while keeping Linux single-host + systemd-first as the primary operations stance.
- Added English companion docs for hybrid quickstart, portability parameters, architecture guide, and implementation rationale, and aligned cross-language links in README/docs.
- Narrowed CI default permissions to `contents: read` and scoped `contents: write` to the test job that updates the coverage badge.
- Added `discord_dedupe_state_file` to `RuntimeConfig` and unified dedupe path usage across notification/incident/test flows by explicit injection.
- Removed implicit dedupe-state path resolution from `discord_client.py` and made dedupe path an explicit argument path for alert/recovery/test/notification send paths.
- Moved `--test-discord` path handling onto runtime config construction so dedupe state follows the same `--config`-based runtime directory resolution.
- Split Gmail runtime logic into dedicated modules:
  - `gmail_auth.py` for OAuth/credential/refresh/auth-state transitions
  - `gmail_transient_state.py` for transient/token issue lifecycle state management
  - kept `gmail_client.py` as compatibility facade and orchestration entry.
- Clarified runtime artifact semantics in README/docs (source-of-truth vs derived snapshot vs rebuildable cache vs coordination/lock).
- Added operations triage order documentation for runtime artifacts and `--rebuild-indexes` usage.
- Added domain-intent comments to StreamingPull event aggregation/duplicate-skip/heartbeat write paths, and aligned hybrid architecture docs with that model.

### Tests
- Added regression tests for stale-state recovery dedupe and cross-notification dedupe behavior (duplicate suppression, in-flight suppression, and per-message-key delivery behavior).
- Added coverage-focused Discord dedupe branch tests so CI coverage gate (`--cov-fail-under=90`) remains stable.
- Added tests for malformed dedupe-state entries and transient-threshold clamp behavior.
- Added frontier/backlog regression tests for paginated polling catch-up and checkpoint-not-found fail-safe behavior.

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
- Added `docs/IMPLEMENTATION_RATIONALE_JA.md` to document design intent and adoption/non-adoption rationale.
- Added transient-alert boundary controls (`transient_alert_min_duration_seconds`, `transient_alert_cooldown_seconds`) so short self-healing failures do not page operators.
- Added optional JSON structured logging via `structured_logging=true`.
- Added `deployment/systemd/install-systemd.sh` and `make install-systemd` for one-command systemd bootstrap.
- Expanded `deployment/systemd/install-systemd.sh` with explicit environment-boundary options (`--base-dir`, `--system-user`, `--config-path`, `--heartbeat-path`) and automatic unit rendering.
- Updated config validation wording so `poll_interval_seconds` minimum behaves as a required lower bound.
- Standardized operational timestamps to UTC ISO 8601 across pipeline runs, checkpoint events, token/transient markers, and failover state.
- Aligned package metadata version with the consolidated release narrative (`version = "0.3.0"` in `pyproject.toml`).
- Enabled Ruff import-order lint (`I`) and normalized imports across the project.
- Added operator docs for portability and reproducible hybrid setup:
  - `docs/HYBRID_QUICKSTART_JA.md`
  - `docs/PORTABILITY_PARAMS_JA.md`.
- Hardened persistence-failure handling around JSONL/state writes:
  - explicit `ENOSPC` hinting in checkpoint/run-result write errors
  - failover-safe handling when failure-event persistence itself fails
  - in-memory incident suppression fallback when incident state writes fail.
- Added exception trace payload support to `JsonLogFormatter` for structured logs.
- Added cross-language documentation links between English/Japanese README variants.

### Tests
- Expanded unit/e2e tests for new streaming/failover/backoff flows.
- Kept coverage gate (`--cov-fail-under=90`) passing with latest implementation.
- Added CLI reconnect behavior regression test.
- Added watchdog regression test for worker heartbeat stale detection.
- Added transient-alert threshold/cooldown behavior tests and recovery-notification boundary tests.
- Added focused `runtime.py` unit tests and restored CI coverage gate compliance (`--cov-fail-under=90`).
- Added fixture-level reset for in-memory incident suppression state to avoid cross-test leakage.

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
