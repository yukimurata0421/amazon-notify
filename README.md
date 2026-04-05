[![CI](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/yukimurata0421/amazon-notify?sort=semver)](https://github.com/yukimurata0421/amazon-notify/releases/latest)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/yukimurata0421/amazon-notify/main/.github/badges/coverage.json)](https://github.com/yukimurata0421/amazon-notify/blob/main/.github/badges/coverage.json)
[![Lint](https://img.shields.io/badge/lint-ruff-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Types](https://img.shields.io/badge/types-mypy-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)

# Amazon Notify

Self-hosted Gmail->Discord notification pipeline for Amazon.co.jp delivery emails.
Built for operational reliability over notification speed.
Design priority: ordered-frontier checkpoint consistency with Gmail inbox state as the recovery source of truth.

Note: the `main` branch may be ahead of the latest GitHub Release.

Two operating modes are supported:
- simple polling for single-host setups
- Gmail Watch + Pub/Sub StreamingPull for near-real-time triggering

Japanese README: [README.ja.md](./README.ja.md)

## Highlights

- Ordered-frontier processing (oldest-first, stop on midstream failure)
- Checkpoint source of truth in `events.jsonl`, with `state.json` compatibility snapshots and `runs.jsonl` audit logs
- Retry and recovery handling for transient Gmail/Discord failures
- Transient-failure alert boundary with persistence threshold and cooldown
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
```

1. Set `discord_webhook_url` in `config.json`
2. Place `credentials.json` next to `config.json`
3. Run OAuth once:

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
```

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

- Hybrid quickstart (copy/paste, common failures): [docs/HYBRID_QUICKSTART_JA.md](./docs/HYBRID_QUICKSTART_JA.md)
- Portability parameters (environment-dependent values): [docs/PORTABILITY_PARAMS_JA.md](./docs/PORTABILITY_PARAMS_JA.md)
- Operations runbook (English): [docs/OPERATIONS.en.md](./docs/OPERATIONS.en.md)
- Hybrid architecture and failover design (detailed): [docs/HYBRID_ARCHITECTURE_JA.md](./docs/HYBRID_ARCHITECTURE_JA.md)
- Engineering decisions and design rationale (English): [docs/engineering-decisions.en.md](./docs/engineering-decisions.en.md)
- Implementation rationale (why these choices were made): [docs/IMPLEMENTATION_RATIONALE_JA.md](./docs/IMPLEMENTATION_RATIONALE_JA.md)
- Japanese full README: [README.ja.md](./README.ja.md)
- Language policy: this README links to English runbook/decision docs; Japanese deep-dive docs are linked separately.
- Optional structured logging (`structured_logging=true`) is supported.

## Security

Do not commit local runtime files:

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `events.jsonl`
- `runs.jsonl`
- `logs/`

## License

MIT License. See [LICENSE](./LICENSE).
