# Operations Guide (English)

For copy/paste hybrid setup, see `docs/HYBRID_QUICKSTART_JA.md`.
For environment-dependent values, see `docs/PORTABILITY_PARAMS_JA.md`.
For design background, see `docs/HYBRID_ARCHITECTURE_JA.md` and `docs/engineering-decisions.en.md`.

## Initial Setup
1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install .`
3. `cp config.example.json config.json`
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

## Failure Handling Summary
- `delivery_failed`: Discord send failed; checkpoint is not advanced.
- `message_detail_failed`: message detail fetch failed; ordered frontier stops at failure point.
- `auth_failed`: token/credential issue; run `amazon-notify --reauth`.
- `checkpoint_failed`: persistence path failed (for example `events.jsonl`/`runs.jsonl` write failure).
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

## Notes
- Current implementation is single-host oriented.
- If you also have external monitoring (node exporter, cloud alerts), use those metrics as primary evidence and cross-check local app logs.
