# Hybrid Quickstart (Pub/Sub + Fallback)

Japanese version: [HYBRID_QUICKSTART_JA.md](./HYBRID_QUICKSTART_JA.md)

This document is the shortest path to run `amazon-notify` with **StreamingPull + fallback watchdog**.
It is written as copy/paste steps with common failure points.

Target environment:
- Linux (Debian/Ubuntu/Raspberry Pi OS)
- systemd
- Gmail API + Pub/Sub in the same GCP project

Assumptions:
- project root: `/opt/amazon-notify` (replace as needed)
- `credentials.json` is already placed

For a full portability checklist, see [PORTABILITY_PARAMS.en.md](./PORTABILITY_PARAMS.en.md).

## 1. Pre-check

```bash
cd /opt/amazon-notify
python3 --version
```

`config.json` must be a **single JSON object**.

```bash
python3 -m json.tool ./config.json >/dev/null && echo CONFIG_JSON_OK
```

## 2. Install Cloud SDK (`gcloud`)

```bash
sudo apt-get update
sudo apt-get install -y apt-transport-https ca-certificates gnupg curl

curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
| sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
| sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null

sudo apt-get update
sudo apt-get install -y google-cloud-cli
gcloud --version
```

## 3. Authenticate (important)

`gcloud auth login` and `gcloud auth application-default login` are different.
Pub/Sub client requires **ADC**.

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project PROJECT_ID
gcloud auth application-default set-quota-project PROJECT_ID
gcloud auth application-default print-access-token >/dev/null && echo ADC_OK
```

## 4. Prepare Pub/Sub

```bash
gcloud services enable pubsub.googleapis.com gmail.googleapis.com

gcloud pubsub topics create amazon-notify-topic
gcloud pubsub subscriptions create amazon-notify-sub --topic amazon-notify-topic

gcloud pubsub topics add-iam-policy-binding amazon-notify-topic \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

## 5. Add Pub/Sub keys to `config.json`

```json
{
  "pubsub_subscription": "projects/PROJECT_ID/subscriptions/amazon-notify-sub",
  "pubsub_main_service_name": "amazon-notify-pubsub.service",
  "pubsub_heartbeat_file": "runtime/pubsub-heartbeat.txt",
  "pubsub_heartbeat_interval_seconds": 30,
  "pubsub_heartbeat_max_age_seconds": 300,
  "pubsub_trigger_failure_max_consecutive": 5,
  "pubsub_trigger_failure_base_delay_seconds": 1.0,
  "pubsub_trigger_failure_max_delay_seconds": 60.0,
  "pubsub_stream_reconnect_base_delay_seconds": 1.0,
  "pubsub_stream_reconnect_max_delay_seconds": 60.0,
  "pubsub_stream_reconnect_max_attempts": 0
}
```

Validate:

```bash
source .venv/bin/activate
amazon-notify --config ./config.json --validate-config
```

## 6. Register Gmail watch

```bash
amazon-notify --config ./config.json --setup-watch \
  --pubsub-topic projects/PROJECT_ID/topics/amazon-notify-topic
```

## 7. Manual StreamingPull check

```bash
amazon-notify --config ./config.json --streaming-pull
```

Expected logs:
- `STREAMING_PULL_MODE_START`
- `RUN_ONCE_*` entries

## 8. Install hybrid systemd units

```bash
sudo bash deployment/systemd/install-systemd.sh \
  --mode hybrid \
  --base-dir /opt/amazon-notify \
  --system-user your_user \
  --config-path /opt/amazon-notify/config.json \
  --heartbeat-path /opt/amazon-notify/runtime/pubsub-heartbeat.txt
```

Check status:

```bash
sudo systemctl status amazon-notify-pubsub.service --no-pager -l
sudo systemctl status amazon-notify-fallback.timer --no-pager -l
```

## 9. Acceptance check

1. Send one test mail that matches your Amazon filter.
2. Verify Discord notification arrives.
3. Optional fallback check:

```bash
sudo systemctl stop amazon-notify-pubsub.service
sudo systemctl start amazon-notify-fallback.service
sudo journalctl -u amazon-notify-fallback.service -n 100 --no-pager
sudo systemctl start amazon-notify-pubsub.service
```

## 10. Common failures

- `GMAIL_WATCH_SETUP_FAILED ... Resource not found`
  - Cause: missing topic or wrong project
  - Fix: set project, create topic, retry
- `DefaultCredentialsError`
  - Cause: ADC not configured
  - Fix: `gcloud auth application-default login`
- `status=217/USER` (systemd)
  - Cause: unresolved `User=YOUR_USER`
  - Fix: set real user in unit
- JSON `Extra data`
  - Cause: multiple JSON objects concatenated in `config.json`
  - Fix: keep one object only

## 11. Operations note

- Gmail watch expires (typically ~7 days). Re-run `--setup-watch` before expiry.
- Useful logs:

```bash
sudo journalctl -u amazon-notify-pubsub.service -f
sudo journalctl -u amazon-notify-fallback.service -f
amazon-notify --config ./config.json --health-check
```

- If `dedupe_lock_supported` is `false`, Discord dedupe lock is unavailable on this platform (Linux + `fcntl` expected).
- When `--config` changes, dedupe-state resolution (`.discord_dedupe_state.json`) also changes accordingly, including `--test-discord`.
- If `events.jsonl` / `runs.jsonl` reads look stale or inconsistent, rebuild indexes with `amazon-notify --config ./config.json --rebuild-indexes`.
