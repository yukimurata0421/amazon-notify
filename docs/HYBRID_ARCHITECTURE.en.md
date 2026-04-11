# Hybrid Architecture Guide (Pub/Sub + Fallback Polling)

Japanese version: [HYBRID_ARCHITECTURE_JA.md](./HYBRID_ARCHITECTURE_JA.md)

This document describes design intent and implementation policy for high-availability operation with `amazon-notify`.

## 1. Goals

1. Low latency: notify Discord as soon as possible after email arrival
2. Resilience: recover notifications even if the main path stops/silences
3. Operability: keep alerts useful and reduce operator noise

## 2. Topology

- Main realtime path: `amazon-notify --streaming-pull` as a systemd service
- Fallback path: `amazon-notify --once --fallback-watchdog` as a systemd timer
- Shared state: `events.jsonl` (checkpoint source of truth) + `state.json` (compat snapshot)

## 3. Failure-detection layers

Layer A (systemd):
- `Restart=always`
- `RestartSec=10`
- optional `OnFailure=amazon-notify-alert@%n.service`

Layer B (fallback watchdog):
- checks main service active state
- checks heartbeat freshness
- runs fallback polling only when unhealthy

Layer C (application level):
- retry transient Gmail/Discord failures with backoff
- alert on persistent/auth failures
- record failure kind in `events.jsonl` and `runs.jsonl`
- reconnect StreamingPull in-process before relying on systemd restart
- polling catch-up scans paginated Gmail listing and keeps checkpoint fail-safe when checkpoint is not found in listing windows

## 4. Silent-stall mitigation (heartbeat)

A key risk in StreamingPull is "process alive but functionally stalled".
Main service updates heartbeat periodically.
Fallback judges stale heartbeat as degraded and can trigger polling recovery.

Suggested defaults:
- heartbeat update interval: `30` seconds
- stale threshold: `300` seconds

## 5. Why duplicate processing risk is manageable

This project uses ordered frontier processing and checkpoint commit semantics.
Even if fallback runs during partial overlap, boundary consistency is designed to remain stable:

Additional context:
- Pub/Sub is treated as a trigger path, not as a durable workflow queue.
- Latest-event aggregation in StreamingPull is a local backlog simplification under the Gmail catch-up model, not a durability guarantee by itself.
- Frontier consistency remains anchored by Gmail state and `events.jsonl`.

- advance frontier only on success
- do not advance checkpoint on midstream failure
- fallback can recover without creating checkpoint holes

## 6. Core CLI

- Main: `amazon-notify --streaming-pull --pubsub-subscription ...`
- Watch setup: `amazon-notify --setup-watch --pubsub-topic ...`
- Fallback: `amazon-notify --once --fallback-watchdog`

## 7. Unit templates

- `deployment/systemd/amazon-notify-pubsub.service`
- `deployment/systemd/amazon-notify-fallback.service`
- `deployment/systemd/amazon-notify-fallback.timer`
- `deployment/systemd/amazon-notify-alert@.service`

## 8. Install flow (summary)

1. set `discord_webhook_url` and `pubsub_subscription` in `config.json`
2. place `credentials.json`, run `amazon-notify --reauth`
3. run `--setup-watch` once
4. install/reload systemd units
5. enable `amazon-notify-pubsub.service` and `amazon-notify-fallback.timer`

## 9. Monitoring checklist

- `journalctl -u amazon-notify-pubsub.service -f`
- `journalctl -u amazon-notify-fallback.service -f`
- `amazon-notify --health-check`
- continuous `checkpoint_advanced` events in `events.jsonl`
- no persistent bias in `runs.jsonl.failure_kind`

## 10. Troubleshooting

- Suspected Pub/Sub trigger silence:
  - check heartbeat timestamps
  - check fallback logs (`SKIP` vs `FAILOVER`)
- No notifications:
  - verify webhook with `--test-discord`
  - verify patterns in `config.json`
  - inspect `delivery_failed` / `auth_failed` in `events.jsonl`
- Auth errors:
  - run `amazon-notify --reauth`
  - verify `credentials.json` / `token.json` location

## 11. Practical operating choices

- minimal start: polling only
- higher availability: StreamingPull + fallback timer
- lower alert noise:
  - incident suppression
  - restart-storm detection
  - duplicate suppression for fallback alerts
