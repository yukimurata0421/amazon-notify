# Minimal Docker Guide (English)

This container target is intentionally small and limited.

## Positioning
- Primary production path: Linux single-host + systemd-first operations.
- Docker path: reproducible quick-start and local verification for the core CLI/runtime behavior.
- This is "Docker-capable", not "Docker-first architecture".

## Included
- Python runtime
- `amazon-notify` package
- Required default pip dependencies from `pyproject.toml`
- CLI execution path for:
  - `amazon-notify --help`
  - `amazon-notify --validate-config`
  - `amazon-notify --once --dry-run`

## Explicitly Out of Scope
- `systemd` operations
- hybrid HA setup
- watchdog/fallback orchestration
- production-grade persistent-volume architecture
- production monitoring/restart-policy tuning
- multi-container composition
- production secret-management architecture for Discord/Gmail

## Build
```bash
docker build -t amazon-notify:slim .
```

## Use GHCR image
On tagged releases, the same Dockerfile is published to GHCR. If you fork or build locally, substitute your registry namespace and tag; no fixed host filesystem path is required.

```bash
docker pull ghcr.io/yukimurata0421/amazon-notify:v0.4.0
docker run --rm ghcr.io/yukimurata0421/amazon-notify:v0.4.0 --help
```

## Try Commands
### 1) Help
```bash
docker run --rm amazon-notify:slim --help
```

### 2) Validate config
`amazon-notify` resolves runtime paths relative to the directory containing `config.json`.

```bash
docker run --rm \
  -v "$(pwd):/work" \
  amazon-notify:slim \
  --config /work/config.json \
  --validate-config
```

### 3) One-shot dry run
```bash
docker run --rm \
  -v "$(pwd):/work" \
  amazon-notify:slim \
  --config /work/config.json \
  --once --dry-run
```

## Host Responsibilities
- Provide and manage `config.json`, `credentials.json`, and `token.json` on the host.
- Manage logs/runtime artifacts lifecycle on the host.
  - Example artifacts: `events.jsonl.checkpoint.index.json`, `runs.jsonl.summary.index.json`, `.discord_dedupe_state.json`.
- Own production operations (`systemd`, watchdog policy, monitoring, restart strategy) outside this thin container scope.
- When `--config` is changed, dedupe-state resolution also follows that runtime directory (including `--test-discord`).
