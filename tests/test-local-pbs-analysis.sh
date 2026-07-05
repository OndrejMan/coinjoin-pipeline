#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FIXTURE_ARCHIVE="${PBS_TEST_FIXTURE:-${SCRIPT_DIR}/pbs-overactive-local-emulation.zip}"
PBS_HELPER="${SCRIPT_DIR}/support/pbs/local-pbs.sh"
PBS_ENV="${SCRIPT_DIR}/support/pbs/pbs-env.sh"
PBS_CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs-analysis-itest-$$}"
RUN_ID="pbs-overactive-local-$RANDOM-$$"
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/gitlab-runner}"
[[ -d "${STORAGE_BASE}" && -w "${STORAGE_BASE}" ]] || {
  echo "FAIL: pre-provisioned writable storage is required: ${STORAGE_BASE}" >&2
  exit 2
}
WORK_DIR="$(mktemp -d "${STORAGE_BASE}/pbs-analysis-${RUN_ID}.XXXXXX")"
RUN_DIR="${WORK_DIR}/${RUN_ID}"
BITCOIN_DATADIR="${WORK_DIR}/bitcoin-regtest-data"
RESULT_DIR="${TEST_RESULT_DIR:-}"

docker_cmd() {
  if /usr/bin/docker info >/dev/null 2>&1; then
    /usr/bin/docker "$@"
  else
    sudo /usr/bin/docker "$@"
  fi
}

cleanup() {
  local status=$?
  if [[ -n "${RESULT_DIR}" ]]; then
    mkdir -p "${RESULT_DIR}/fixture/pbs-logs"
    find "${WORK_DIR}" -type f \( -name '*.o[0-9]*' -o -name '*.e[0-9]*' \) \
      -exec cp -t "${RESULT_DIR}/fixture/pbs-logs" {} + 2>/dev/null || true
  fi
  if (( status != 0 )); then
    echo "PBS fixture test failed; collected job output follows:" >&2
    while IFS= read -r output; do
      echo "===== ${output} =====" >&2
      tail -n 200 "${output}" >&2 || true
    done < <(find "${WORK_DIR}" -type f \( -name '*.o[0-9]*' -o -name '*.e[0-9]*' \) -print 2>/dev/null)
  fi
  docker_cmd rm -f "${RUN_ID}-btc" >/dev/null 2>&1 || true
  docker_cmd rm -f "${PBS_CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker_cmd run --rm --user root -v "${WORK_DIR}:/test-work" \
    --entrypoint chmod "${BTC_NODE_IMAGE:-ghcr.io/ondrejman/btc-node:latest}" \
    -R a+rwX /test-work >/dev/null 2>&1 || true
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

for command in docker unzip python3; do
  command -v "${command}" >/dev/null 2>&1 || { echo "FAIL: missing ${command}" >&2; exit 2; }
done
[[ -s "${FIXTURE_ARCHIVE}" ]] || { echo "FAIL: missing fixture ${FIXTURE_ARCHIVE}" >&2; exit 2; }

mkdir -p "${RUN_DIR}" "${BITCOIN_DATADIR}"
unzip -q "${FIXTURE_ARCHIVE}" -d "${WORK_DIR}/fixture"
FIXTURE_DATA="$(find "${WORK_DIR}/fixture" -type d -path '*/coinjoin_emulator_data/data' -print -quit)"
[[ -n "${FIXTURE_DATA}" ]] || { echo "FAIL: fixture has no emulator data" >&2; exit 1; }
mkdir -p "${RUN_DIR}/coinjoin_emulator_data"
cp -a "${FIXTURE_DATA}" "${RUN_DIR}/coinjoin_emulator_data/data"
cp "$(dirname "${FIXTURE_DATA}")/scenario.json" "${RUN_DIR}/coinjoin_emulator_data/scenario.json"
chmod -R a+rwX "${WORK_DIR}"

echo "Reconstructing the fixture's Bitcoin Core datadir..."
docker_cmd run -d --name "${RUN_ID}-btc" -p 127.0.0.1::18443 \
  -v "${BITCOIN_DATADIR}:/home/bitcoin/data:rw" --entrypoint bitcoind \
  "${BTC_NODE_IMAGE:-ghcr.io/ondrejman/btc-node:latest}" \
  -datadir=/home/bitcoin/data -regtest -server=1 -rpcbind=0.0.0.0 \
  -rpcallowip=0.0.0.0/0 -rpcuser=user -rpcpassword=password -blocksxor=0 >/dev/null
RPC_PORT="$(docker_cmd port "${RUN_ID}-btc" 18443/tcp | sed -nE 's/.*:([0-9]+)$/\1/p' | head -n 1)"
python3 "${PROJECT_DIR}/import-emulation-blocks.py" \
  "${RUN_DIR}/coinjoin_emulator_data/data/btc-node" \
  --rpc-url "http://127.0.0.1:${RPC_PORT}" --rpc-user user --rpc-pass password
docker_cmd stop "${RUN_ID}-btc" >/dev/null
docker_cmd run --rm --user root -v "${BITCOIN_DATADIR}:/bitcoin-data" \
  --entrypoint chmod "${BTC_NODE_IMAGE:-ghcr.io/ondrejman/btc-node:latest}" \
  -R a+rX /bitcoin-data >/dev/null

export PBS_CONTAINER_NAME PBS_WORKDIR_HOST="${WORK_DIR}" PBS_WORKDIR_CONTAINER="${WORK_DIR}"
"${PBS_HELPER}" start

source "${PBS_ENV}"
export PBS_CLIENT_WORKDIR="${WORK_DIR}"
export EMULATION_LOGS_DIR="${WORK_DIR}"
export PBS_FRONTEND_DIRECT=1

echo "Running coinjoin-analysis through the documented runIt.sh PBS interface..."
(cd "${PROJECT_DIR}" && ./runIt.sh coinjoin-analysis \
  --run-dir "${RUN_DIR}" --analysisPbs --pbs-ncpus 1 --pbs-mem 2gb --pbs-scratch 1gb --pbs-walltime 00:10:00)

echo "Running BlockSci through the documented runIt.sh PBS interface..."
(cd "${PROJECT_DIR}" && ./runIt.sh analyze --engine wasabi \
  --run-dir "${RUN_DIR}" --blocksciPbs --pbs-bitcoin-datadir "${BITCOIN_DATADIR}" \
  --pbs-ncpus 1 --pbs-mem 4gb --pbs-scratch 1gb --pbs-walltime 00:15:00)

python3 - "${RUN_DIR}" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
baseline = json.loads((run_dir / "coinjoin-analysis_data/coinjoin_tx_info.json").read_text())
report = json.loads((run_dir / "blocksciEmulatorAnalysis_data/unified_report.json").read_text())
if not baseline:
    raise SystemExit("FAIL: coinjoin-analysis returned no records")
if (report.get("run") or {}).get("coinjoin_type") != "wasabi2":
    raise SystemExit("FAIL: BlockSci report is not for wasabi2")
summary = report.get("summary") or {}
if "blocksci_agreement_rate" not in summary or not report.get("transactions"):
    raise SystemExit("FAIL: unified report has no analyzer comparison metrics")
print(f"PASS: validated {len(baseline)} baseline records and the BlockSci comparison report")
PY

if [[ -n "${RESULT_DIR}" ]]; then
  mkdir -p "${RESULT_DIR}/fixture"
  cp "${RUN_DIR}/blocksciEmulatorAnalysis_data/unified_report.json" "${RESULT_DIR}/fixture/"
  cp "${RUN_DIR}/blocksciEmulatorAnalysis_data/unified_report.md" "${RESULT_DIR}/fixture/"
  cp "${RUN_DIR}/coinjoin-analysis_data/coinjoin_tx_info.json" "${RESULT_DIR}/fixture/"
fi
