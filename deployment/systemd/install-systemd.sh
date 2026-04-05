#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MODE="hybrid"
ENABLE_NOW="1"
INSTALL_DEPS="1"

usage() {
  cat <<'USAGE'
Usage:
  sudo deployment/systemd/install-systemd.sh [--mode standard|hybrid] [--no-enable-now] [--no-install-deps]

Options:
  --mode              Install units for `standard` (polling only) or `hybrid` (pubsub + fallback timer).
  --no-enable-now     Install units but do not run `systemctl enable --now`.
  --no-install-deps   Skip virtualenv/pip installation.
  -h, --help          Show this help.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --no-enable-now)
      ENABLE_NOW="0"
      shift
      ;;
    --no-install-deps)
      INSTALL_DEPS="0"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "${MODE}" != "standard" && "${MODE}" != "hybrid" ]]; then
  echo "--mode must be standard or hybrid" >&2
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

if [[ "${INSTALL_DEPS}" == "1" ]]; then
  if [[ ! -d "${REPO_DIR}/.venv" ]]; then
    python3 -m venv "${REPO_DIR}/.venv"
  fi
  "${REPO_DIR}/.venv/bin/python" -m pip install --upgrade pip
  if [[ "${MODE}" == "hybrid" ]]; then
    "${REPO_DIR}/.venv/bin/pip" install -e "${REPO_DIR}[pubsub]"
  else
    "${REPO_DIR}/.venv/bin/pip" install -e "${REPO_DIR}"
  fi
fi

install -m 0644 "${SCRIPT_DIR}/amazon-notify.service" /etc/systemd/system/amazon-notify.service
install -m 0644 "${SCRIPT_DIR}/amazon-notify-alert@.service" /etc/systemd/system/amazon-notify-alert@.service
chmod +x "${SCRIPT_DIR}/notify-on-failure.sh"

if [[ ! -f "${SCRIPT_DIR}/amazon-notify-alert.env" ]]; then
  install -m 0644 "${SCRIPT_DIR}/amazon-notify-alert.env.example" "${SCRIPT_DIR}/amazon-notify-alert.env"
fi

if [[ "${MODE}" == "hybrid" ]]; then
  install -m 0644 "${SCRIPT_DIR}/amazon-notify-pubsub.service" /etc/systemd/system/amazon-notify-pubsub.service
  install -m 0644 "${SCRIPT_DIR}/amazon-notify-fallback.service" /etc/systemd/system/amazon-notify-fallback.service
  install -m 0644 "${SCRIPT_DIR}/amazon-notify-fallback.timer" /etc/systemd/system/amazon-notify-fallback.timer
fi

systemctl daemon-reload

if [[ "${ENABLE_NOW}" == "1" ]]; then
  if [[ "${MODE}" == "hybrid" ]]; then
    systemctl enable --now amazon-notify-pubsub.service
    systemctl enable --now amazon-notify-fallback.timer
  else
    systemctl enable --now amazon-notify.service
  fi
fi

echo "Install complete."
echo "Mode: ${MODE}"
echo "Repo: ${REPO_DIR}"
echo "Next step: verify User/WorkingDirectory/ExecStart values in /etc/systemd/system/*.service"
