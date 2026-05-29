#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="netquota"
AGENT_NAME="netquota-agent.py"
SERVICE_NAME="netquota.service"
TIMER_NAME="netquota.timer"

INSTALL_BIN="/usr/local/bin/${AGENT_NAME}"
CONFIG_FILE="/etc/netquota-agent.conf"
STATE_DIR="/var/lib/netquota-agent"
SYSTEMD_DIR="/etc/systemd/system"

ENABLE_TIMER=true
START_TIMER=true
FORCE_CONFIG=false
UNINSTALL=false

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SRC="${SCRIPT_DIR}/${AGENT_NAME}"
CONFIG_SRC="${SCRIPT_DIR}/netquota-agent.conf.sample"
SERVICE_SRC="${SCRIPT_DIR}/${SERVICE_NAME}"
TIMER_SRC="${SCRIPT_DIR}/${TIMER_NAME}"

log() {
  printf '[%s] %s\n' "${APP_NAME}" "$*"
}

warn() {
  printf '[%s] WARN: %s\n' "${APP_NAME}" "$*" >&2
}

die() {
  printf '[%s] ERROR: %s\n' "${APP_NAME}" "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage:
  sudo ./install.sh [options]

Options:
  --no-enable      Install files but do not enable netquota.timer.
  --no-start       Install files but do not start netquota.timer now.
  --force-config   Overwrite /etc/netquota-agent.conf from the sample file.
  --uninstall      Remove installed agent and systemd units. Keeps config/state.
  -h, --help       Show this help.

Environment variables used to prefill /etc/netquota-agent.conf:
  WORKER_URL        Cloudflare Worker URL, for example https://netquota.example.workers.dev
  AUTH_TOKEN        Shared bearer token. Must match Worker secret AUTH_TOKEN.
  NODE_ID           Logical node id. Defaults to hostname when empty.
  INTERFACES        Comma-separated interfaces, or auto. Default: auto
  RESET_DAY         Monthly reset day in UTC, 1-31. Default: 1
  RESET_HOUR_UTC    Monthly reset hour in UTC, 0-23. Default: 0
  EXPIRE_AT         Unix timestamp, YYYY-MM-DD, ISO time, or 0. Default: 0
  TOTAL_BYTES       Total quota in bytes. Default: 0
  BILLING_MODE      both, upload, or download. Default: both

Example:
  sudo WORKER_URL="https://netquota.example.workers.dev" \
       AUTH_TOKEN="change-this-token" \
       NODE_ID="vps-1" \
       RESET_DAY="1" \
       TOTAL_BYTES="8796093022208" \
       ./install.sh
USAGE
}

parse_args() {
  while (($#)); do
    case "$1" in
      --no-enable)
        ENABLE_TIMER=false
        ;;
      --no-start)
        START_TIMER=false
        ;;
      --force-config)
        FORCE_CONFIG=true
        ;;
      --uninstall)
        UNINSTALL=true
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
    shift
  done
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "please run as root, for example: sudo ./install.sh"
  fi
}

check_platform() {
  [[ "$(uname -s)" == "Linux" ]] || die "Linux is required"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
  command -v systemctl >/dev/null 2>&1 || die "systemd/systemctl is required"
  [[ -d /sys/class/net ]] || die "/sys/class/net is required to read NIC counters"
}

check_sources() {
  [[ -f "${AGENT_SRC}" ]] || die "missing ${AGENT_SRC}"
  [[ -f "${CONFIG_SRC}" ]] || die "missing ${CONFIG_SRC}"
  [[ -f "${SERVICE_SRC}" ]] || die "missing ${SERVICE_SRC}"
  [[ -f "${TIMER_SRC}" ]] || die "missing ${TIMER_SRC}"
}

install_files() {
  log "installing agent to ${INSTALL_BIN}"
  install -D -m 0755 "${AGENT_SRC}" "${INSTALL_BIN}"

  log "installing systemd units"
  install -D -m 0644 "${SERVICE_SRC}" "${SYSTEMD_DIR}/${SERVICE_NAME}"
  install -D -m 0644 "${TIMER_SRC}" "${SYSTEMD_DIR}/${TIMER_NAME}"

  install -d -m 0755 "${STATE_DIR}"
  install -d -m 0755 "$(dirname "${CONFIG_FILE}")"
}

create_or_refresh_config() {
  if [[ -f "${CONFIG_FILE}" && "${FORCE_CONFIG}" != "true" ]]; then
    log "keeping existing ${CONFIG_FILE}"
  else
    if [[ -f "${CONFIG_FILE}" ]]; then
      log "overwriting ${CONFIG_FILE}"
    else
      log "creating ${CONFIG_FILE}"
    fi
    install -m 0600 "${CONFIG_SRC}" "${CONFIG_FILE}"
  fi

  chmod 0600 "${CONFIG_FILE}"
}

get_config_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {print substr($0, length(key) + 2); exit}' "${CONFIG_FILE}"
}

set_config_value() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  awk -v key="${key}" -v value="${value}" '
    BEGIN { done = 0 }
    $0 ~ "^[[:space:]]*" key "=" {
      print key "=" value
      done = 1
      next
    }
    { print }
    END {
      if (!done) {
        print key "=" value
      }
    }
  ' "${CONFIG_FILE}" > "${tmp}"
  cat "${tmp}" > "${CONFIG_FILE}"
  rm -f "${tmp}"
}

set_default_if_empty() {
  local key="$1"
  local value="$2"
  if [[ -z "$(get_config_value "${key}")" ]]; then
    set_config_value "${key}" "${value}"
  fi
}

apply_env_config() {
  local key
  for key in WORKER_URL AUTH_TOKEN NODE_ID INTERFACES RESET_DAY RESET_HOUR_UTC EXPIRE_AT TOTAL_BYTES BILLING_MODE STATE_FILE REQUEST_TIMEOUT INCLUDE_HOSTNAME; do
    if [[ -n "${!key:-}" ]]; then
      set_config_value "${key}" "${!key}"
    fi
  done
}

prompt_if_interactive() {
  [[ -t 0 && -t 1 ]] || return 0

  local current input

  current="$(get_config_value WORKER_URL)"
  if [[ -z "${current}" ]]; then
    read -r -p "Cloudflare Worker URL: " input
    [[ -n "${input}" ]] && set_config_value WORKER_URL "${input}"
  fi

  current="$(get_config_value AUTH_TOKEN)"
  if [[ -z "${current}" ]]; then
    read -r -s -p "AUTH_TOKEN: " input
    printf '\n'
    [[ -n "${input}" ]] && set_config_value AUTH_TOKEN "${input}"
  fi

  current="$(get_config_value NODE_ID)"
  read -r -p "NODE_ID [${current:-$(hostname)}]: " input
  [[ -n "${input}" ]] && set_config_value NODE_ID "${input}"
}

normalize_config_defaults() {
  set_default_if_empty INTERFACES "auto"
  set_default_if_empty RESET_DAY "1"
  set_default_if_empty RESET_HOUR_UTC "0"
  set_default_if_empty EXPIRE_AT "0"
  set_default_if_empty TOTAL_BYTES "0"
  set_default_if_empty BILLING_MODE "both"
  set_default_if_empty STATE_FILE "${STATE_DIR}/state.json"
  set_default_if_empty REQUEST_TIMEOUT "10"
  set_default_if_empty INCLUDE_HOSTNAME "true"
}

config_ready() {
  [[ -n "$(get_config_value WORKER_URL)" && -n "$(get_config_value AUTH_TOKEN)" ]]
}

reload_systemd() {
  log "reloading systemd"
  systemctl daemon-reload
}

enable_and_start() {
  if ! config_ready; then
    warn "WORKER_URL or AUTH_TOKEN is empty; timer will not be enabled or started"
    warn "edit ${CONFIG_FILE}, then run: sudo systemctl enable --now ${TIMER_NAME}"
    return 0
  fi

  if [[ "${ENABLE_TIMER}" == "true" ]]; then
    log "enabling ${TIMER_NAME}"
    systemctl enable "${TIMER_NAME}"
  fi

  if [[ "${START_TIMER}" == "true" ]]; then
    log "starting ${TIMER_NAME}"
    systemctl start "${TIMER_NAME}"
  fi
}

print_summary() {
  cat <<SUMMARY

Installed ${APP_NAME}.

Files:
  Agent:   ${INSTALL_BIN}
  Config:  ${CONFIG_FILE}
  Service: ${SYSTEMD_DIR}/${SERVICE_NAME}
  Timer:   ${SYSTEMD_DIR}/${TIMER_NAME}
  State:   ${STATE_DIR}/state.json

Useful commands:
  sudo systemctl status ${TIMER_NAME}
  sudo systemctl list-timers ${TIMER_NAME}
  sudo ${INSTALL_BIN} status
  sudo ${INSTALL_BIN} sample

SUMMARY
}

uninstall() {
  log "stopping and disabling ${TIMER_NAME}"
  systemctl disable --now "${TIMER_NAME}" >/dev/null 2>&1 || true

  log "removing installed files"
  rm -f "${INSTALL_BIN}" "${SYSTEMD_DIR}/${SERVICE_NAME}" "${SYSTEMD_DIR}/${TIMER_NAME}"
  systemctl daemon-reload

  cat <<SUMMARY
Removed ${APP_NAME} agent and systemd units.

Kept:
  ${CONFIG_FILE}
  ${STATE_DIR}

Remove them manually if you no longer need local configuration or traffic state.
SUMMARY
}

main() {
  parse_args "$@"
  require_root
  check_platform

  if [[ "${UNINSTALL}" == "true" ]]; then
    uninstall
    exit 0
  fi

  check_sources
  install_files
  create_or_refresh_config
  apply_env_config
  prompt_if_interactive
  normalize_config_defaults
  reload_systemd
  enable_and_start
  print_summary
}

main "$@"
