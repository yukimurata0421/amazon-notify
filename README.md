[![CI](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/yukimurata0421/amazon-notify?sort=semver)](https://github.com/yukimurata0421/amazon-notify/releases/latest)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/yukimurata0421/amazon-notify/main/.github/badges/coverage.json)](https://github.com/yukimurata0421/amazon-notify/blob/main/.github/badges/coverage.json)
[![Lint](https://img.shields.io/badge/lint-ruff-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Types](https://img.shields.io/badge/types-mypy-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)

# Amazon Notify

Amazon Notify watches Gmail for Amazon.co.jp delivery emails and posts them to Discord.
It is designed for recovery-safe processing rather than maximum speed.
A message is considered processed only after the notification succeeds, and catch-up always relies on Gmail inbox state.
Target platform: Linux single-host deployment, systemd-first operations.

Two operating modes are supported:
- polling for single-host setups
- Gmail Watch + Pub/Sub StreamingPull for near-real-time triggering

Japanese README: [README.ja.md](./README.ja.md)

## Version Track

| Track | Description |
|---|---|
| `main` branch | Latest implementation (may be ahead of a release tag) |
| Latest release | Last tagged release on GitHub |
| Next intended release | `0.4.0` (see `CHANGELOG.md` Unreleased) |

## Behavior Example

1. Message `A` is notified successfully -> checkpoint advances to `A`.
2. Message `B` notification fails -> run stops at `B`.
3. Checkpoint remains at `A` (no forward hole).
4. Next run resumes from `B` and continues oldest-first.

## Highlights

Note: the `main` branch may be ahead of the latest GitHub Release.

- Ordered-frontier processing (oldest-first, stop on midstream failure)
- Checkpoint source of truth in `events.jsonl`, with `state.json` compatibility snapshots and `runs.jsonl` audit logs
- Rebuildable index snapshots (`events.jsonl.checkpoint.index.json`, `runs.jsonl.summary.index.json`) to keep startup/runtime-status reads fast on long-lived files
- Retry and recovery handling for transient Gmail/Discord failures
- Transient-failure alert boundary with persistence threshold and cooldown
- Unhandled guard-path exceptions are normalized into persisted `RunResult`/`source_failed` records
- Discord dedupe lock is fail-fast on non-`fcntl` platforms (also exposed via `--health-check` as `dedupe_lock_supported`)
- Realtime mode with Pub/Sub StreamingPull
- In-process StreamingPull self-healing:
  - trigger failure backoff/circuit-breaker
  - stream session reconnect backoff (before systemd restart)
- Hybrid HA mode:
  - Main: StreamingPull service
  - Fallback: timer-based polling with watchdog (`systemd` + heartbeat)
- `systemd` restart storm protection and OnFailure alert hook

## Guarantees

- A message is treated as processed only after the notification path succeeds.
- Ordered frontier consistency is preserved (oldest-first, stop on midstream failure).
- Pub/Sub is treated as a trigger path; Gmail inbox state remains the catch-up source.

## Non-goals

- Multi-instance distributed processing.
- Per-Pub/Sub-message durable workflow tracking.
- Generic mail forwarding platform.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
cp config.example.json config.json
# use config.full.example.json if you need Pub/Sub / advanced retry knobs
```

1. Set `discord_webhook_url` in `config.json`
2. Place `credentials.json` next to `config.json`
3. Run `amazon-notify --reauth` and complete the browser OAuth flow when prompted.
4. Run one-shot verification:

```bash
amazon-notify --reauth
amazon-notify --once
```

## Common Commands

```bash
# loop polling
amazon-notify

# single run
amazon-notify --once

# dry run (no Discord post, no checkpoint commit)
amazon-notify --once --dry-run

# realtime streaming pull
amazon-notify --streaming-pull --pubsub-subscription projects/PROJECT/subscriptions/SUB

# setup Gmail watch
amazon-notify --setup-watch --pubsub-topic projects/PROJECT/topics/TOPIC

# fallback watchdog single run
amazon-notify --once --fallback-watchdog

# rebuild index snapshots from current events/runs files
amazon-notify --rebuild-indexes
```

## Try With Minimal Docker

```bash
docker build -t amazon-notify:slim .
docker run --rm amazon-notify:slim --help
docker run --rm -v "$(pwd):/work" amazon-notify:slim --config /work/config.json --validate-config
docker run --rm -v "$(pwd):/work" amazon-notify:slim --config /work/config.json --once --dry-run
```

Positioning:
- Primary production path: Linux single-host + systemd-first operations.
- Docker path: local evaluation and reproducible testing of the CLI/runtime boundary.

Scope note: this minimal image is for CLI bring-up only. It does not include `systemd`, hybrid HA/watchdog orchestration, multi-container operations, or production secret/monitoring design.

## Health Check Notes

- `amazon-notify --health-check` includes `dedupe_lock_supported`.
- On platforms without `fcntl`, this check becomes `false` and dedupe lock paths are treated as unsupported (fail-fast).

## Optional Dependencies

Pub/Sub mode:

```bash
pip install -e .[pubsub]
```

Development:

```bash
pip install -e .[dev]
```

## Documentation

Run it:
- Hybrid quickstart (English): [docs/HYBRID_QUICKSTART.en.md](./docs/HYBRID_QUICKSTART.en.md)
- Hybrid quickstart (Japanese): [docs/HYBRID_QUICKSTART_JA.md](./docs/HYBRID_QUICKSTART_JA.md)

Operate it:
- Operations runbook (English): [docs/OPERATIONS.en.md](./docs/OPERATIONS.en.md)
- Operations runbook (Japanese): [docs/OPERATIONS.md](./docs/OPERATIONS.md)
- Portability parameters (English): [docs/PORTABILITY_PARAMS.en.md](./docs/PORTABILITY_PARAMS.en.md)
- Portability parameters (Japanese): [docs/PORTABILITY_PARAMS_JA.md](./docs/PORTABILITY_PARAMS_JA.md)

Understand design:
- Engineering decisions and design rationale (English): [docs/engineering-decisions.en.md](./docs/engineering-decisions.en.md)
- Engineering decisions and design rationale (Japanese): [docs/engineering-decisions.md](./docs/engineering-decisions.md)
- Implementation rationale (English): [docs/IMPLEMENTATION_RATIONALE.en.md](./docs/IMPLEMENTATION_RATIONALE.en.md)
- Implementation rationale (Japanese): [docs/IMPLEMENTATION_RATIONALE_JA.md](./docs/IMPLEMENTATION_RATIONALE_JA.md)
- Hybrid architecture and failover design (English): [docs/HYBRID_ARCHITECTURE.en.md](./docs/HYBRID_ARCHITECTURE.en.md)
- Hybrid architecture and failover design (Japanese): [docs/HYBRID_ARCHITECTURE_JA.md](./docs/HYBRID_ARCHITECTURE_JA.md)

Docker:
- Minimal Docker guide (English): [docs/DOCKER.en.md](./docs/DOCKER.en.md)
- Minimal Docker guide (Japanese): [docs/DOCKER.md](./docs/DOCKER.md)

- Japanese full README: [README.ja.md](./README.ja.md)
- Language policy: operations, Docker, and engineering-decision docs are maintained in both English (`*.en.md`) and Japanese (`*.md`). This README prioritizes English links and also includes Japanese counterparts.
- Optional structured logging (`structured_logging=true`) is supported.

## Security

Do not commit local runtime files:

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `events.jsonl`
- `events.jsonl.checkpoint.index.json`
- `runs.jsonl`
- `runs.jsonl.summary.index.json`
- `.state.json.lock`
- `.discord_dedupe_state.json`
- `.discord_dedupe_state.lock`
- `logs/`

## License

MIT License. See [LICENSE](./LICENSE).
