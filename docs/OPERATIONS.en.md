# Operations Guide (English)

For copy/paste hybrid setup, see `docs/HYBRID_QUICKSTART.en.md` (or Japanese: `docs/HYBRID_QUICKSTART_JA.md`).
For environment-dependent values, see `docs/PORTABILITY_PARAMS.en.md` (or Japanese: `docs/PORTABILITY_PARAMS_JA.md`).
For design background, see `docs/HYBRID_ARCHITECTURE.en.md` and `docs/engineering-decisions.en.md`.

## Initial Setup
1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install .`
3. `cp config.example.json config.json`
   - If you need Pub/Sub or advanced retry knobs, start from `config.full.example.json`.
4. Set `discord_webhook_url` in `config.json`
5. Place `credentials.json` next to `config.json`
6. Run `amazon-notify --reauth`
7. Verify with `amazon-notify --once`

## Common Operations
- Polling loop: `amazon-notify`
- StreamingPull mode: `amazon-notify --streaming-pull --pubsub-subscription projects/PROJECT_ID/subscriptions/SUBSCRIPTION_ID`
- Setup Gmail watch: `amazon-notify --setup-watch --pubsub-topic projects/PROJECT_ID/topics/TOPIC_ID`
- Fallback watchdog one-shot: `amazon-notify --once --fallback-watchdog`
- Dry run: `amazon-notify --once --dry-run`
- Validate config: `amazon-notify --validate-config`
- Health check JSON: `amazon-notify --health-check`

## Runtime Files and Paths
- Relative paths (`state_file`, `events_file`, `runs_file`, `log_file`) are resolved from the directory containing `config.json`.
- Use `--config /path/to/config.json` when operating from another working directory.
- Runtime-derived artifacts to keep local (do not commit):
  - `events.jsonl.checkpoint.index.json`
  - `runs.jsonl.summary.index.json`
  - `.state.json.lock`
  - `.discord_dedupe_state.json`
  - `.discord_dedupe_state.lock`

## Health Check Signals
- `amazon-notify --health-check` includes `dedupe_lock_supported`.
- If `dedupe_lock_supported=false`, file-lock-based Discord dedupe is unavailable on the current platform.

## Runtime Artifact Triage Order
- Check frontier source of truth first: `events.jsonl`
  - confirm `checkpoint_advanced` progression is as expected.
- Check per-run status next: `runs.jsonl`
  - inspect latest `failure_kind`, `checkpoint_before`, and `checkpoint_after`.
- Check compatibility snapshot: `state.json`
  - use it as a derived compatibility view, not the checkpoint authority.
- Suspect stale/corrupted index cache when reads look inconsistent:
  - rebuild `events.jsonl.checkpoint.index.json` / `runs.jsonl.summary.index.json` with `amazon-notify --rebuild-indexes`.
- Check Discord dedupe coordination state:
  - inspect `.discord_dedupe_state.json` and `.discord_dedupe_state.lock`.
- Check lock/coordination contention:
  - inspect `.state.json.lock` and `.discord_dedupe_state.lock`.

## v0.4.0 Migration Notes
- Checkpoint source of truth is `events.jsonl` (`checkpoint_advanced`).
- `state.json` remains a derived compatibility snapshot.
- On first startup only, if `events.jsonl` is empty and `state.json.last_message_id` exists, one bootstrap `checkpoint_advanced` event is written.
- Discord dedupe state is unified under the runtime directory (`.discord_dedupe_state.json`).
  - When `--config` changes, `--test-discord`, normal notification, alert, and recovery all use the same runtime-anchored dedupe state path.
- Index snapshots (`events.jsonl.checkpoint.index.json` / `runs.jsonl.summary.index.json`) are rebuildable caches.
  - If startup/read behavior looks stale or inconsistent, run `amazon-notify --rebuild-indexes`.
- Polling catch-up scans paginated Gmail listing. If checkpoint is not visible in listing windows, checkpoint is not advanced past unknown frontier (fail-safe).
- If `transient_alert_min_duration_seconds` is negative, runtime handling clamps it to `0` with a warning instead of aborting.
- Guard-path unhandled exceptions converge to persisted `source_failed` + `RunResult`, so standard operations checks (`events.jsonl` / `runs.jsonl`) still apply.
- Rollback notes:
  - `state.json` keeps being updated, so 0.1-series boundary data is preserved.
  - 0.2+ audit logs (`events.jsonl` / `runs.jsonl`) are not consumed by 0.1-series binaries.

## Failure Handling Summary
- `delivery_failed`: Discord send failed; checkpoint is not advanced.
- `message_detail_failed`: message detail fetch failed; ordered frontier stops at failure point.
- `auth_failed`: token/credential issue; run `amazon-notify --reauth`.
- `checkpoint_failed`: persistence path failed (for example `events.jsonl`/`runs.jsonl` write failure).
- `source_failed`: source-side failure or an unexpected runtime error path.
- Short-lived transient failures that never crossed the alert threshold are silently cleared, so no recovery notification is sent in that case.

## Disk Full / ENOSPC Operations
### Symptoms
- Log contains `JSONL_WRITE_FAILED`.
- `checkpoint_failed` error contains `ENOSPC` or `No space left on device`.

### Behavior
- Checkpoint advancement is stopped on the safe side (consistency first).
- If run-result persistence fails, the run is treated as `checkpoint_failed` and enters alert flow.
- If incident state writes fail, in-memory suppression is used as fallback to reduce repeated alerts.

### First Response
```bash
df -h
df -i
du -sh logs events.jsonl runs.jsonl
sudo systemctl restart amazon-notify-pubsub.service
```

## systemd (Hybrid)
Recommended units:
- `amazon-notify-pubsub.service`
- `amazon-notify-fallback.service`
- `amazon-notify-fallback.timer`

Check status:
```bash
sudo systemctl status amazon-notify-pubsub.service
sudo systemctl status amazon-notify-fallback.timer
sudo journalctl -u amazon-notify-pubsub.service -f
sudo journalctl -u amazon-notify-fallback.service -f
```

## JSONL Maintenance (Long-Running Deployments)

- Rebuild index snapshots:

```bash
amazon-notify --config ./config.json --rebuild-indexes
```

- Archive `events.jsonl` / `runs.jsonl` example:
  - stop running services
  - copy and compress archives
  - rebuild indexes if needed

```bash
sudo systemctl stop amazon-notify-pubsub.service amazon-notify-fallback.timer
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p archive
cp events.jsonl "archive/events-${ts}.jsonl"
cp runs.jsonl "archive/runs-${ts}.jsonl"
gzip -f "archive/events-${ts}.jsonl" "archive/runs-${ts}.jsonl"
amazon-notify --config ./config.json --rebuild-indexes
sudo systemctl start amazon-notify-pubsub.service amazon-notify-fallback.timer
```

## Notes
- Current implementation is single-host oriented.
- Linux + `fcntl` is the supported lock environment for Discord dedupe coordination.
- If you also have external monitoring (node exporter, cloud alerts), use those metrics as primary evidence and cross-check local app logs.

## Release Checklist
Run this fixed pre-release gate before cutting a release:

```bash
make release-check
```

What it executes:
- `ruff check .`
- `ruff format --check .`
- `mypy .`
- `pytest -q --cov=amazon_notify --cov-report=term-missing --cov-report=xml --cov-fail-under=90`
- `docker build -t amazon-notify:0.4.0 .`
- `docker run --rm -v "$(pwd):/work" amazon-notify:0.4.0 --config /work/config.example.json --validate-config`

## Manual update and rollback (no auto deploy)
This repository intentionally does not auto-deploy to production hosts. Updates and rollback are manual.

Update:
1. Checkout the target tag (example: `v0.4.0`).
2. Reinstall package dependencies (`pip install .`).
3. Restart services and verify status.

```bash
git fetch --tags
git checkout v0.4.0
source .venv/bin/activate
pip install .
sudo systemctl restart amazon-notify-pubsub.service amazon-notify-fallback.timer
sudo systemctl status amazon-notify-pubsub.service
```

Rollback:
1. Checkout the previous stable tag.
2. Run `pip install .` again.
3. Restart services, then verify `events.jsonl`, `runs.jsonl`, and logs.

```bash
git checkout v0.3.0
source .venv/bin/activate
pip install .
sudo systemctl restart amazon-notify-pubsub.service amazon-notify-fallback.timer
```
