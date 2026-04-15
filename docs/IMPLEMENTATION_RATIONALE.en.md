# Implementation Rationale (Design Perspective)

Japanese version: [IMPLEMENTATION_RATIONALE_JA.md](./IMPLEMENTATION_RATIONALE_JA.md)

This document records why recent `amazon-notify` changes were adopted.
Focus is on consistency, recoverability, and maintainability.
For stable architecture-level decisions, see `docs/engineering-decisions.en.md`.

## 1. Prioritization criteria

1. Preserve consistency contracts
  - ordered frontier (stop on midstream failure)
  - advance checkpoint only after successful notification
2. Prefer self-healing before restart
  - retry/reconnect in-process when possible
  - keep systemd restart as final fallback
3. Alert mainly on sustained failures
  - avoid noise for short transient glitches
  - notify on persistent failures
4. Encode operations in code
  - CLI options, scripts, validation, and tests
5. Favor current reliability over speculative expansion
  - avoid overdesign when single-host constraints are sufficient

## 2. Adopted decisions

### 2.0 Current update scope
- Consolidated `GmailMailSource` injection points behind a typed `GmailClient` protocol and `GmailClientAdapter`.
- Replaced production-path `assert` usage in retry/incident flows with explicit guards for runtime-safe behavior under optimized execution.
- Moved incident in-memory suppression ownership from mutable `RuntimeConfig` state to notifier-managed process cache keyed by `state_file`.
- Unified StreamingPull trigger success/failure/heartbeat/backoff handling into `_run_trigger_once` to avoid branch drift.
- Removed legacy `TypeError` fallback shims from Gmail alert/recovery dedupe wrappers and enforced explicit keyword-argument contract.
- Added regression coverage for paginated checkpoint boundaries and concurrent Discord dedupe claim suppression.
- Unified Discord dedupe-state path handling via runtime path injection (`--config`-anchored resolution).
- Split Gmail runtime logic by responsibility (`gmail_auth.py` and `gmail_transient_state.py`) while keeping `gmail_client.py` as compatibility facade.
- Added explicit runtime-artifact role documentation in README/operations docs (source-of-truth vs derived vs rebuildable cache vs coordination/lock).
- Added domain-intent comments in StreamingPull for history aggregation, duplicate skip behavior, and heartbeat atomic-write rationale.
- Hardened polling catch-up with paginated listing and checkpoint-not-found fail-safe behavior.
- Changed negative `transient_alert_min_duration_seconds` handling to warning + clamp (`0`) instead of runtime abort.
- Tightened Discord dedupe-state parsing/pruning so malformed inflight entries are explicitly dropped.
- Bound Gmail source loop lambdas with default arguments to avoid future closure-capture pitfalls.
- Applied least-privilege CI permission defaults (`contents: read` by default, `contents: write` only for coverage-badge update job).

### 2.1 Hybrid topology (Pub/Sub main + polling fallback)
- main: StreamingPull service for low latency
- fallback: timer-based polling for recovery
- watchdog decision based on service status + heartbeat freshness

### 2.2 systemd as final fallback
- reconnect/backoff handled in-process first
- systemd restart remains the last safety net

### 2.3 Strict alert boundaries
- `transient_alert_min_duration_seconds`
- `transient_alert_cooldown_seconds`
- silent clear when threshold was never crossed

### 2.4 Checkpoint source-of-truth write order
- write `events.jsonl` first
- update `state.json` as best-effort snapshot

### 2.5 JSONL durability hardening
- `flush + fsync` for append writes
- atomic `state.json` writes (`tempfile + os.replace`)
- fail fast on middle corruption, tolerate tail-only corruption

### 2.6 Runtime path dependency reduction
- extend `RuntimePaths` injection
- reduce reliance on mutable global paths

### 2.7 Additional quality improvements
- compile patterns once at startup
- `cache_discovery=False` for Gmail API client build
- optional structured logging (`structured_logging=true`)
- automated systemd bootstrap script

## 3. Not adopted (with reasons)

### 3.1 SQL/SQLite migration
Not adopted now.
Current single-host frontier model is covered by JSONL with lower operational overhead.

Revisit when:
- multi-instance concurrency becomes a requirement
- JSONL I/O becomes a real bottleneck
- complex query/analytics requirements become mandatory

### 3.2 Generic DLQ / durable resend queue
Deferred.
Current checkpoint semantics already provide safe retry behavior with lower complexity.

## 4. Error-handling boundaries

- Transient:
  - in-process retry/backoff
  - no alert below threshold
- Persistent:
  - alert path
  - fallback recovery path remains active
- Fatal/Auth:
  - immediate alert
  - `--reauth` recovery path
  - checkpoint advancement stops
- Process dead / silent stall:
  - watchdog can trigger fallback
  - final recovery delegated to systemd

## 5. Current conclusion

Current optimization target is:
- keep consistency contracts intact
- keep alert noise low
- recover automatically when possible
- keep operation steps reproducible
