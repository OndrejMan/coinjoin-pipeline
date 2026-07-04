#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR:-${SCRIPT_DIR}/emulation_logs}"
COMPOSE_FILE="${SCRIPT_DIR}/btc-rpc-explorer.compose.yaml"
ENV_FILE="${SCRIPT_DIR}/.btc-rpc-explorer.env"
IMPORTER="${SCRIPT_DIR}/import-emulation-blocks.py"

usage() {
  cat <<'USAGE'
Usage:
  ./run-btc-rpc-explorer.sh [RUN_DIR]
  ./run-btc-rpc-explorer.sh --latest
  ./run-btc-rpc-explorer.sh --rpc-host HOST [--rpc-port PORT] [--rpc-user USER --rpc-pass PASS]
  ./run-btc-rpc-explorer.sh down

By default, RUN_DIR is the newest directory under emulation_logs that contains
blocksciEmulatorAnalysis_data/unified_report.json. The helper starts a regtest bitcoind, imports the
emulator's coinjoin_emulator_data/data/btc-node/block_*.json files into a per-report Docker volume,
then connects BTC RPC Explorer to that node.

Options:
  --latest              Use the newest report directory. This is the default.
  --port PORT           Host port for BTC RPC Explorer. Default: 3002.
  --rpc-host HOST       Connect to an already-running bitcoind RPC host.
  --rpc-port PORT       bitcoind RPC port. Default: 18443.
  --rpc-user USER       bitcoind RPC username.
  --rpc-pass PASS       bitcoind RPC password.
  --volume NAME         Docker volume for reconstructed Bitcoin Core datadir.
  --cookie PATH         Cookie path inside the mounted datadir. Default: /bitcoin/regtest/.cookie.
  --no-start            Only write .btc-rpc-explorer.env; do not start compose.
  down                  Stop the explorer compose stack.
USAGE
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    echo "ERROR: neither 'docker compose' nor 'docker-compose' is available." >&2
    exit 2
  fi
}

compose_env_args() {
  if [[ -f "${ENV_FILE}" ]]; then
    printf '%s\n' "--env-file" "${ENV_FILE}"
  fi
}

latest_run_dir() {
  find "${EMULATION_LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d \
    -exec test -f "{}/blocksciEmulatorAnalysis_data/unified_report.json" \; -print |
    sort |
    tail -n 1
}

write_env() {
  local run_dir="$1"
  local run_id
  local compose_rpc_host="${RPC_HOST}"

  run_id="$(basename "${run_dir}" | tr -c '[:alnum:]_.-' '-')"

  if [[ "${compose_rpc_host}" == "127.0.0.1" || "${compose_rpc_host}" == "localhost" || "${compose_rpc_host}" == "::1" ]]; then
    compose_rpc_host="host.docker.internal"
  fi

  if [[ -z "${BTC_DATA_VOLUME}" ]]; then
    BTC_DATA_VOLUME="bitcoin-analysis-btc-data-${run_id}"
  fi

  {
    printf 'BTC_RPC_EXPLORER_HOST_PORT=%s\n' "${EXPLORER_PORT}"
    printf 'BTCEXP_UI_TIMEZONE=%s\n' "${BTCEXP_UI_TIMEZONE:-Europe/Prague}"
    printf 'BTCEXP_BITCOIND_PORT=%s\n' "${RPC_PORT}"
    printf 'BTCEXP_BITCOIND_COOKIE=%s\n' "${COOKIE_PATH}"
    printf 'BTCEXP_BTC_DATA_VOLUME=%s\n' "${BTC_DATA_VOLUME}"

    if [[ -n "${RPC_HOST}" ]]; then
      printf 'BTCEXP_BITCOIND_HOST=%s\n' "${compose_rpc_host}"
      printf 'BTCEXP_BITCOIND_USER=%s\n' "${RPC_USER}"
      printf 'BTCEXP_BITCOIND_PASS=%s\n' "${RPC_PASS}"
    else
      printf 'BTCEXP_BITCOIND_HOST=%s\n' "bitcoind"
      printf 'BTCEXP_BITCOIND_USER=%s\n' "user"
      printf 'BTCEXP_BITCOIND_PASS=%s\n' "password"
    fi

    printf 'BITCOIN_ANALYSIS_RUN_DIR=%s\n' "${run_dir}"
  } > "${ENV_FILE}"
}

RUN_DIR=""
RPC_HOST="${BTCEXP_BITCOIND_HOST:-}"
RPC_PORT="${BTCEXP_BITCOIND_PORT:-18443}"
RPC_USER="${BTCEXP_BITCOIND_USER:-}"
RPC_PASS="${BTCEXP_BITCOIND_PASS:-}"
COOKIE_PATH="${BTCEXP_BITCOIND_COOKIE:-/bitcoin/regtest/.cookie}"
EXPLORER_PORT="${BTC_RPC_EXPLORER_HOST_PORT:-3002}"
BTC_DATA_VOLUME="${BTCEXP_BTC_DATA_VOLUME:-}"
START_COMPOSE=true

if [[ $# -gt 0 && "$1" == "down" ]]; then
  mapfile -t ENV_ARGS < <(compose_env_args)
  compose_cmd "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" down
  exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --latest)
      RUN_DIR=""
      shift
      ;;
    --port)
      EXPLORER_PORT="$2"
      shift 2
      ;;
    --port=*)
      EXPLORER_PORT="${1#--port=}"
      shift
      ;;
    --rpc-host)
      RPC_HOST="$2"
      shift 2
      ;;
    --rpc-host=*)
      RPC_HOST="${1#--rpc-host=}"
      shift
      ;;
    --rpc-port)
      RPC_PORT="$2"
      shift 2
      ;;
    --rpc-port=*)
      RPC_PORT="${1#--rpc-port=}"
      shift
      ;;
    --rpc-user)
      RPC_USER="$2"
      shift 2
      ;;
    --rpc-user=*)
      RPC_USER="${1#--rpc-user=}"
      shift
      ;;
    --rpc-pass)
      RPC_PASS="$2"
      shift 2
      ;;
    --rpc-pass=*)
      RPC_PASS="${1#--rpc-pass=}"
      shift
      ;;
    --volume)
      BTC_DATA_VOLUME="$2"
      shift 2
      ;;
    --volume=*)
      BTC_DATA_VOLUME="${1#--volume=}"
      shift
      ;;
    --cookie)
      COOKIE_PATH="$2"
      shift 2
      ;;
    --cookie=*)
      COOKIE_PATH="${1#--cookie=}"
      shift
      ;;
    --no-start)
      START_COMPOSE=false
      shift
      ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      RUN_DIR="$1"
      shift
      ;;
  esac
done

if [[ -z "${RUN_DIR}" ]]; then
  RUN_DIR="$(latest_run_dir)"
fi

if [[ -z "${RUN_DIR}" || ! -d "${RUN_DIR}" ]]; then
  echo "ERROR: no emulation report directory found under ${EMULATION_LOGS_DIR}" >&2
  exit 2
fi

RUN_DIR="$(cd "${RUN_DIR}" && pwd)"
BTC_NODE_DIR="${RUN_DIR}/coinjoin_emulator_data/data/btc-node"

if [[ ! -d "${BTC_NODE_DIR}" ]]; then
  echo "ERROR: expected btc-node data directory at ${BTC_NODE_DIR}" >&2
  exit 2
fi

if [[ -z "${RPC_HOST}" && ! -x "${IMPORTER}" ]]; then
  chmod +x "${IMPORTER}"
fi

write_env "${RUN_DIR}"

echo "Using run: ${RUN_DIR}"
echo "Wrote: ${ENV_FILE}"
if [[ -n "${RPC_HOST}" && "${RPC_HOST}" != "$(grep '^BTCEXP_BITCOIND_HOST=' "${ENV_FILE}" | cut -d= -f2-)" ]]; then
  echo "Mapped RPC host ${RPC_HOST} to host.docker.internal for Docker networking"
fi

if [[ "${START_COMPOSE}" == "false" ]]; then
  exit 0
fi

mapfile -t ENV_ARGS < <(compose_env_args)
if [[ -n "${RPC_HOST}" ]]; then
  compose_cmd "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" up -d --no-deps btc-rpc-explorer
else
  echo "Using Bitcoin Core volume: ${BTC_DATA_VOLUME}"
  compose_cmd "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" up -d bitcoind
  "${IMPORTER}" "${BTC_NODE_DIR}" \
    --rpc-url "http://127.0.0.1:${RPC_PORT}" \
    --rpc-user user \
    --rpc-pass password
  compose_cmd "${ENV_ARGS[@]}" -f "${COMPOSE_FILE}" up -d btc-rpc-explorer
fi

echo "BTC RPC Explorer: http://localhost:${EXPLORER_PORT}"
