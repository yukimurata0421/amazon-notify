#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MODE="hybrid"
ENABLE_NOW="1"
INSTALL_DEPS="1"
BASE_DIR="${REPO_DIR}"
SYSTEM_USER="${SUDO_USER:-$(id -un)}"
CONFIG_PATH=""
HEARTBEAT_PATH=""

usage() {
  cat <<'USAGE'
Usage:
  sudo deployment/systemd/install-systemd.sh \
    [--mode standard|hybrid] \
    [--base-dir PATH] \
    [--system-user USER] \
    [--config-path PATH] \
    [--heartbeat-path PATH] \
    [--no-enable-now] \
    [--no-install-deps]

Options:
  --mode              Install units for `standard` (polling only) or `hybrid` (pubsub + fallback timer).
  --base-dir          Base directory used in systemd units (default: repository root).
  --system-user       Linux user for systemd User= (default: SUDO_USER or current user).
  --config-path       config.json absolute path in ExecStart (default: <base-dir>/config.json).
  --heartbeat-path    heartbeat file absolute path in ExecStart (default: <base-dir>/runtime/pubsub-heartbeat.txt).
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
    --base-dir)
      BASE_DIR="${2:-}"
      shift 2
      ;;
    --system-user)
      SYSTEM_USER="${2:-}"
      shift 2
      ;;
    --config-path)
      CONFIG_PATH="${2:-}"
      shift 2
      ;;
    --heartbeat-path)
      HEARTBEAT_PATH="${2:-}"
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

if [[ -z "${SYSTEM_USER}" ]] || ! id -u "${SYSTEM_USER}" >/dev/null 2>&1; then
  echo "Invalid --system-user: ${SYSTEM_USER}" >&2
  exit 1
fi

BASE_DIR="$(realpath -m "${BASE_DIR}")"
if [[ ! -d "${BASE_DIR}" ]]; then
  echo "--base-dir does not exist: ${BASE_DIR}" >&2
  exit 1
fi

DEFAULT_CONFIG_PATH="${BASE_DIR}/config.json"
DEFAULT_HEARTBEAT_PATH="${BASE_DIR}/runtime/pubsub-heartbeat.txt"
CONFIG_PATH="${CONFIG_PATH:-${DEFAULT_CONFIG_PATH}}"
HEARTBEAT_PATH="${HEARTBEAT_PATH:-${DEFAULT_HEARTBEAT_PATH}}"

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

render_and_install_unit() {
  local src="$1"
  local dest="$2"
  local tmp

  local user_esc base_esc default_cfg_esc default_hb_esc cfg_esc hb_esc
  user_esc="$(escape_sed_replacement "${SYSTEM_USER}")"
  base_esc="$(escape_sed_replacement "${BASE_DIR}")"
  default_cfg_esc="$(escape_sed_replacement "${DEFAULT_CONFIG_PATH}")"
  default_hb_esc="$(escape_sed_replacement "${DEFAULT_HEARTBEAT_PATH}")"
  cfg_esc="$(escape_sed_replacement "${CONFIG_PATH}")"
  hb_esc="$(escape_sed_replacement "${HEARTBEAT_PATH}")"

  tmp="$(mktemp)"
  sed \
    -e "s/^User=YOUR_USER$/User=${user_esc}/" \
    -e "s|/opt/amazon-notify|${base_esc}|g" \
    -e "s|${default_cfg_esc}|${cfg_esc}|g" \
    -e "s|${default_hb_esc}|${hb_esc}|g" \
    "${src}" >"${tmp}"

  install -m 0644 "${tmp}" "${dest}"
  rm -f "${tmp}"
}

if [[ "${INSTALL_DEPS}" == "1" ]]; then
  if [[ ! -d "${BASE_DIR}/.venv" ]]; then
    python3 -m venv "${BASE_DIR}/.venv"
  fi
  "${BASE_DIR}/.venv/bin/python" -m pip install --upgrade pip
  if [[ "${MODE}" == "hybrid" ]]; then
    "${BASE_DIR}/.venv/bin/pip" install -e "${BASE_DIR}[pubsub]"
  else
    "${BASE_DIR}/.venv/bin/pip" install -e "${BASE_DIR}"
  fi
fi

if [[ ! -f "${BASE_DIR}/deployment/systemd/notify-on-failure.sh" ]]; then
  echo "notify-on-failure.sh not found under ${BASE_DIR}/deployment/systemd" >&2
  exit 1
fi
chmod +x "${BASE_DIR}/deployment/systemd/notify-on-failure.sh"

if [[ ! -f "${BASE_DIR}/deployment/systemd/amazon-notify-alert.env" ]]; then
  install -m 0644 "${SCRIPT_DIR}/amazon-notify-alert.env.example" "${BASE_DIR}/deployment/systemd/amazon-notify-alert.env"
fi

render_and_install_unit "${SCRIPT_DIR}/amazon-notify.service" /etc/systemd/system/amazon-notify.service
render_and_install_unit "${SCRIPT_DIR}/amazon-notify-alert@.service" /etc/systemd/system/amazon-notify-alert@.service

if [[ "${MODE}" == "hybrid" ]]; then
  render_and_install_unit "${SCRIPT_DIR}/amazon-notify-pubsub.service" /etc/systemd/system/amazon-notify-pubsub.service
  render_and_install_unit "${SCRIPT_DIR}/amazon-notify-fallback.service" /etc/systemd/system/amazon-notify-fallback.service
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
echo "Base dir: ${BASE_DIR}"
echo "System user: ${SYSTEM_USER}"
echo "Config path: ${CONFIG_PATH}"
echo "Heartbeat path: ${HEARTBEAT_PATH}"
