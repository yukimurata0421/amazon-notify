#!/usr/bin/env bash
set -eu

unit_name="${1:-amazon-notify.service}"
webhook_url="${DISCORD_ALERT_WEBHOOK_URL:-}"

if [ -z "$webhook_url" ]; then
  echo "DISCORD_ALERT_WEBHOOK_URL is empty; skip alert."
  exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; skip alert."
  exit 0
fi

unit_result="$(systemctl show -p Result --value "$unit_name" 2>/dev/null || true)"

if [ "$unit_result" != "start-limit-hit" ]; then
  echo "Unit result is '$unit_result'; skip alert."
  exit 0
fi

host_name="$(hostname 2>/dev/null || echo unknown-host)"
timestamp="$(date '+%Y-%m-%d %H:%M:%S %Z')"
content="amazon-notify restart storm detected on ${host_name} at ${timestamp}. unit=${unit_name} result=${unit_result}"

json_payload="$(printf '%s' "$content" | sed 's/\\/\\\\/g; s/"/\\"/g; s/^/{\"content\":\"/; s/$/\"}/')"

curl --fail --silent --show-error \
  -H "Content-Type: application/json" \
  -d "$json_payload" \
  "$webhook_url"
