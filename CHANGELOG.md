# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- Documented long-term JSONL lifecycle in operations guides (JA/EN): rotation policy vs append-only authority, archive layout, restore steps, safe-to-delete table, and periodic restore drill; rationale captured in `docs/engineering-decisions.md` (§22).
- Added composite fault scenario tests (`tests/scenarios/test_fault_scenarios.py`) for JSONL corruption, index rebuild, stale incident vs event log, and checkpoint persistence failure; rationale in `docs/engineering-decisions.md` (§23).
- Added `--verify-state` (alias of `--doctor` JSON) for scheduled consistency checks; added `--metrics` / `--metrics-plain` / `--metrics-window` for thin operational export (checkpoint age, recent run stats, dedupe/incident summaries); rationale in `docs/engineering-decisions.md` (§24).
- Added `time_utils.parse_utc_iso()` for metrics and timestamp parsing.
- Documented path/layout independence in README and related docs (config-directory-relative paths, `--config`, placeholder install paths); rationale in `docs/engineering-decisions.md` (§25–26).
- Added explicit review follow-up tracker: `docs/review-followup-2026-04-18.md` (status table by review item, including rationale for each decision).
- Added tag-based Release workflow (`.github/workflows/release.yml`) that:
  - requires a successful CI run for the tagged commit
  - builds distributable artifacts (`dist/amazon-notify.zip`, wheel, sdist)
  - creates a GitHub Release with body extracted from the matching `CHANGELOG.md` section.
- Added tag-based GHCR publish workflow (`.github/workflows/ghcr.yml`) that:
  - requires a successful CI run for the tagged commit
  - builds and publishes Docker images to `ghcr.io/<owner>/amazon-notify`.
- Added runtime operator diagnostics commands:
  - `--status` for a thin one-shot summary (frontier, last success, incident status, last failure, consistency overview)
  - `--doctor` for detailed JSON diagnostics across `state/events/runs/index` consistency checks.

### Added
- `amazon_notify/notification_bridge.py`: extracted Discord dedupe notification wrappers from `gmail_client.py` to separate Gmail boundary concerns from Discord-specific logic.
- `amazon_notify/backoff.retry_with_backoff()`: generic retry utility with exponential back-off, replacing duplicated retry loops in `gmail_source.py` and `gmail_client.py`.
- `RuntimeConfig` sub-configs (`GmailApiConfig`, `DiscordRetryConfig`, `PubSubConfig`, `TransientAlertConfig`) with backward-compatible flat-attribute access via `__getattr__`.
- `PersistentState` TypedDict in `domain.py` for compile-time key-name validation of the JSON state dict.
- `mask_webhook_url()` in `runtime.py` to redact webhook tokens from log output.
- `_flock_with_timeout()` in `discord_client.py` for bounded file-lock acquisition (10 s default).
- Pipeline component caching in `notifier._PIPELINE_CACHE` to avoid redundant object creation across `run_once` calls.
- `.github/dependabot.yml` for automated pip and GitHub Actions dependency updates.
- CI `pip` cache (`cache: pip` in `actions/setup-python@v6`) for faster dependency installation.

### Changed
- Expanded Docker docs (JA/EN) with GHCR usage examples for tagged images.
- Expanded operations docs (JA/EN) with explicit manual update and rollback procedures, keeping production deploy out of CI/CD scope.
- Refactored `GmailMailSource` dependency wiring into a protocol-based adapter (`GmailClientAdapter`) to reduce constructor bloat and centralize Gmail boundary injection.
- Simplified `GmailClientAdapter` to delegate via `__getattr__` instead of explicit per-method wrappers.
- Removed production-path `assert` dependencies in retry/incident flows and replaced them with explicit guard handling to keep behavior stable under optimized runtime flags.
- Simplified StreamingPull trigger execution by consolidating duplicated idle/event trigger success-failure handling into a shared execution path.
- Moved incident in-memory suppression ownership out of `RuntimeConfig` mutable state into notifier-managed process cache keyed by runtime state file, keeping per-runtime isolation without config mutation.
- Tightened Discord dedupe alert/recovery sender contract by removing legacy `TypeError` fallback shims in `gmail_client.py`; test doubles now follow the explicit keyword-argument API.
- Narrowed `except Exception` in `discord_client._post_webhook` to `except requests.exceptions.RequestException` to avoid swallowing programming errors.
- Added docstring to `amazon_notify/commands/__init__.py` explaining the DI-seam purpose of the command layer.
- Refactored `NotificationPipeline.run_once()` by extracting phase helpers for envelope processing, pipeline-error handling, unexpected-error handling, result building, and result persistence.
- Switched Discord webhook transport to a module-level `requests.Session` with split timeout `(connect, read)`.
- Added `DeprecationWarning` on `RuntimeConfig.__getattr__` flat-attribute access to guide migration toward sub-config access (`runtime.gmail_api.*`, `runtime.pubsub.*`, etc.).

### Tests
- Added pagination-boundary regression tests to verify correct oldest-first processing when checkpoint appears on a later Gmail listing page.
- Added checkpoint-not-found multi-page regression test to ensure fail-safe frontier preservation when boundary is absent from listing windows.
- Added concurrent dedupe claim test to confirm in-flight suppression prevents duplicate Discord posts under same-content concurrent sends.
- Updated token/transient recovery test doubles to validate the explicit dedupe keyword-argument contract for Gmail alert/recovery wrappers.
- Added `tests/unit/test_review_additions.py` with coverage for: non-retryable request exceptions, truncated JSONL recovery, `max_messages` truncation, incident memory map isolation, `mask_webhook_url`, `retry_with_backoff` validation, and `time-machine` migration demo.

## [0.4.0] - 2026-04-07

Summary:
- Runtime state handling was reorganized around explicit path injection, incident-state storage separation, and index rebuild tooling.
- StreamingPull internals were split for maintainability while preserving existing runtime behavior and health/heartbeat outputs.
- Documentation was expanded in Japanese/English, including hybrid setup, portability notes, and a thin Docker evaluation path.
- Discord dedupe path resolution was aligned with runtime path injection, and Gmail runtime logic was split into auth/transient-state modules while preserving the public API surface.

### Changed
- Added a global Discord notification dedupe layer (alert/recovery/test/delivery) with idempotency keys, cross-process lock coordination, and in-flight claim handling to suppress duplicate sends under concurrent runtimes.
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
- Extracted `handle_test_discord()` and added top-level action conflict validation in CLI dispatch (`--setup-watch/--rebuild-indexes`, `--setup-watch/--test-discord`, `--health-check/--test-discord`, `--validate-config/--rebuild-indexes`).
- Unified `state.json` read-modify-write locking across checkpoint snapshots, incident lifecycle state updates, and Gmail transient/token state tracking through a shared `state_update_lock` helper.
- Split Gmail runtime logic into dedicated modules:
  - `gmail_auth.py` for OAuth/credential/refresh/auth-state transitions
  - `gmail_transient_state.py` for transient/token issue lifecycle state management
  - kept `gmail_client.py` as compatibility facade and orchestration entry.
- Clarified runtime artifact semantics in README/docs (source-of-truth vs derived snapshot vs rebuildable cache vs coordination/lock).
- Added operations triage order documentation for runtime artifacts and `--rebuild-indexes` usage.
- Added domain-intent comments to StreamingPull event aggregation/duplicate-skip/heartbeat write paths, and aligned hybrid architecture docs with that model.
- Added `project.urls` package metadata and tightened static checks (Ruff `B/UP/RUF`, mypy warning strictness); introduced `make release-check` and CI `ruff format --check`.

### Tests
- Added regression tests for stale-state recovery dedupe and cross-notification dedupe behavior (duplicate suppression, in-flight suppression, and per-message-key delivery behavior).
- Added coverage-focused Discord dedupe branch tests so CI coverage gate (`--cov-fail-under=90`) remains stable.
- Added tests for malformed dedupe-state entries and transient-threshold clamp behavior.
- Added frontier/backlog regression tests for paginated polling catch-up and checkpoint-not-found fail-safe behavior.
- Added contract tests to verify runtime-anchored Discord dedupe path propagation across `--test-discord`, notifier incident lifecycle, and failover watchdog flows.
- Added lock-contract tests for `state_update_lock` usage in checkpoint snapshot and incident store updates, plus CLI action-conflict validation tests.

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
