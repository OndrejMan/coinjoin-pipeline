#!/usr/bin/env bash
# Public Bitcoin fixture -> bitcoin-block-archive Docker image -> MinIO ->
# PBS/Apptainer BlockSci parse, then an incremental update after the archive
# grows, checked against a full parse of the grown archive. Only mainnet
# heights 0-3 are downloaded, so this is a real Core block-file archive
# without a full chain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARCHIVE_PROJECT_DIR="${BITCOIN_BLOCK_ARCHIVE_DIR:-${PROJECT_DIR}/../bitcoin-block-archive}"
PBS_SUPPORT_ROOT="${PBS_SUPPORT_ROOT:-${SCRIPT_DIR}/support/pbs}"
PBS_HELPER="${PBS_HELPER:-${PBS_SUPPORT_ROOT}/local-pbs.sh}"
PBS_ENV="${PBS_ENV:-${PBS_SUPPORT_ROOT}/pbs-env.sh}"

# These checks run before the EXIT trap and before the work root exists, so a
# failure here used to vanish with the terminal output — which is exactly what
# happened in the 2026-09-04 suite, where this test died between two polls and
# left nothing behind. Record preflight failures where the next reader looks.
PREFLIGHT_LOG_DIR="${TEST_FAILED_LOGS_DIR:-${PROJECT_DIR}/emulation_logs/_failed}"

fail_preflight() {
  local message="$1"
  mkdir -p "${PREFLIGHT_LOG_DIR}" 2>/dev/null || true
  printf '%s FAIL(preflight): %s\n' "$(TZ=UTC date -Is)" "${message}" \
    | tee -a "${PREFLIGHT_LOG_DIR}/$(TZ=UTC date +%Y%m%dT%H%M%S)Z-bitcoin-block-archive-preflight.log" >&2
  exit 2
}

docker_ready() {
  # Right after the previous S3 test tears down its k3d cluster and containers,
  # the daemon can refuse `docker info` for a few seconds. One probe turned that
  # into a suite failure, so give it a short grace period.
  local attempt
  for attempt in 1 2 3 4 5; do
    docker info >/dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}

for command in docker python3 timeout; do
  command -v "${command}" >/dev/null 2>&1 || fail_preflight "missing ${command}"
done
docker_ready || fail_preflight "Docker is unavailable after 5 probes over ~15s"
[[ -d "${ARCHIVE_PROJECT_DIR}/src/bitcoin_block_archive" ]] \
  || fail_preflight "bitcoin-block-archive source is unavailable: ${ARCHIVE_PROJECT_DIR}"
[[ -x "${PBS_HELPER}" && -f "${PBS_ENV}" ]] \
  || fail_preflight "local PBS support is unavailable (${PBS_HELPER}, ${PBS_ENV})"

RUN_TOKEN="$(TZ=Europe/Prague date +%Y%m%dT%H%M%S%Z)-$$-${RANDOM}"
RESOURCE_ID="${GITHUB_RUN_ID:-$$}"
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}"
[[ -d "${STORAGE_BASE}" && -w "${STORAGE_BASE}" ]] \
  || fail_preflight "writable /storage root is required: ${STORAGE_BASE}"
WORK_ROOT="$(mktemp -d "${STORAGE_BASE}/bitcoin-block-archive-s3-${RUN_TOKEN}.XXXXXX")"
LOGS_ROOT="${WORK_ROOT}/emulation_logs"
PBS_CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs-block-archive-itest-${RESOURCE_ID}}"
MINIO_CONTAINER_NAME="${MINIO_CONTAINER_NAME:-minio-block-archive-itest-${RESOURCE_ID}}"
S5CMD_IMAGE="${S5CMD_IMAGE:-}"
if [[ -z "${S5CMD_IMAGE}" ]]; then
  S5CMD_IMAGE="$(tr -d '[:space:]' <"${PROJECT_DIR}/container/uploader.image")"
fi
BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
PBS_BLOCKSCI_LOCAL_IMAGE="${PBS_BLOCKSCI_LOCAL_IMAGE:-}"
BITCOIN_BLOCK_ARCHIVE_IMAGE="${BITCOIN_BLOCK_ARCHIVE_IMAGE:-}"
BUILT_BITCOIN_BLOCK_ARCHIVE_IMAGE=0
RESULT_DIR="${TEST_RESULT_DIR:-${PROJECT_DIR}/emulation_logs/_test-results/bitcoin-block-archive-s3-minio-${RUN_TOKEN}}"
E2E_TIMEOUT="${BITCOIN_BLOCK_ARCHIVE_S3_TIMEOUT:-35m}"
ESPLORA_API="${ESPLORA_API:-https://blockstream.info/api}"
RUN_ID="bitcoin-block-archive-e2e-${RUN_TOKEN}"
UPDATE_RUN_ID="${RUN_ID}-u"
FULL_RUN_ID="${RUN_ID}-f"
BUCKET="coinjoin-e2e"
BLOCKS_URI="s3://${BUCKET}/bitcoin-blocks"
ARTIFACT_URI="s3://${BUCKET}/runs"
S3_PROFILE="coinjoin"
MINIO_ROOT_USER="e2e-access-key"
MINIO_ROOT_PASSWORD="e2e-secret-key-${RUN_TOKEN}"
CREDENTIALS_FILE="${WORK_ROOT}/s3-credentials"
S3_ENDPOINT_URL=""
PIPELINE_OUTPUT_FILE="${WORK_ROOT}/pipeline-output.log"
DIAGNOSTICS_FILE="${WORK_ROOT}/diagnostics.txt"

s5() {
  s5cmd --credentials-file "${CREDENTIALS_FILE}" --profile "${S3_PROFILE}" \
    --endpoint-url "${S3_ENDPOINT_URL}" "$@"
}

dump_diagnostics() {
  {
    echo "===== S3 objects ====="; s5 ls "s3://${BUCKET}/*" || true
    echo "===== PBS history ====="; qstat -x 2>/dev/null || true
    echo "===== PBS logs ====="
    if [[ -n "${S3_ENDPOINT_URL}" ]]; then
      s5 cp "${ARTIFACT_URI}/${RUN_ID}/logs/blocksci-parse.pbs.log" \
        "${WORK_ROOT}/blocksci-parse.pbs.log" >/dev/null 2>&1 || true
      s5 cp "${ARTIFACT_URI}/${FULL_RUN_ID}/logs/blocksci-parse.pbs.log" \
        "${WORK_ROOT}/blocksci-full-parse.pbs.log" >/dev/null 2>&1 || true
    fi
    for log in blocksci-parse.pbs.log blocksci-full-parse.pbs.log; do
      [[ ! -s "${WORK_ROOT}/${log}" ]] || { echo "----- ${log}"; tail -n 200 "${WORK_ROOT}/${log}"; }
    done
    find "${LOGS_ROOT}" -type f -name '*.pbs.log' -print -exec tail -n 200 {} \; 2>/dev/null || true
  } >"${DIAGNOSTICS_FILE}" 2>&1
  cat "${DIAGNOSTICS_FILE}" >&2
}

cleanup() {
  local status=$?
  trap - EXIT
  (( status == 0 )) || dump_diagnostics || true
  mkdir -p "${RESULT_DIR}"
  for artifact in blk00000.dat.json blk00001.dat.json blocksci-parse-manifest.json blocksci-update-manifest.json \
    blocksci-full-parse-manifest.json blocksci-parse.pbs.log blocksci-full-parse.pbs.log pipeline-output.log diagnostics.txt; do
    [[ -s "${WORK_ROOT}/${artifact}" ]] && cp "${WORK_ROOT}/${artifact}" "${RESULT_DIR}/${artifact}"
  done
  docker rm -f "${PBS_CONTAINER_NAME}" "${MINIO_CONTAINER_NAME}" >/dev/null 2>&1 || true
  if (( BUILT_BITCOIN_BLOCK_ARCHIVE_IMAGE )); then
    docker image rm "${BITCOIN_BLOCK_ARCHIVE_IMAGE}" >/dev/null 2>&1 || true
  fi
  if [[ "${KEEP_TEST_WORK:-0}" != 1 ]]; then rm -rf "${WORK_ROOT}"; else echo "Keeping ${WORK_ROOT}" >&2; fi
  exit "${status}"
}
trap cleanup EXIT

mkdir -p "${WORK_ROOT}/bin" "${WORK_ROOT}/blocks" "${WORK_ROOT}/later-blocks" "${WORK_ROOT}/state" "${LOGS_ROOT}"
chmod 0777 "${WORK_ROOT}" "${WORK_ROOT}/state" "${LOGS_ROOT}"
if ! docker image inspect "${S5CMD_IMAGE}" >/dev/null 2>&1; then docker pull "${S5CMD_IMAGE}"; fi
S5CMD_CONTAINER="$(docker create "${S5CMD_IMAGE}")"
docker cp "${S5CMD_CONTAINER}:/usr/local/bin/s5cmd" "${WORK_ROOT}/bin/s5cmd"
docker rm -f "${S5CMD_CONTAINER}" >/dev/null
chmod 0755 "${WORK_ROOT}/bin/s5cmd"
export PATH="${WORK_ROOT}/bin:${PATH}"

GATEWAY="${CONTAINER_KUBE_HOST:-$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')}"
docker rm -f "${PBS_CONTAINER_NAME}" "${MINIO_CONTAINER_NAME}" >/dev/null 2>&1 || true
docker run -d --name "${MINIO_CONTAINER_NAME}" \
  -e MINIO_ROOT_USER="${MINIO_ROOT_USER}" -e MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD}" \
  -p 9000 "${MINIO_IMAGE:-pgsty/minio:RELEASE.2026-08-04T00-00-00Z}" server /data >/dev/null
MINIO_PORT="$(docker port "${MINIO_CONTAINER_NAME}" 9000/tcp | head -n 1 | awk -F: '{print $NF}')"
S3_ENDPOINT_URL="http://${GATEWAY}:${MINIO_PORT}"
printf '[%s]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
  "${S3_PROFILE}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >"${CREDENTIALS_FILE}"
for _ in $(seq 1 60); do s5 ls >/dev/null 2>&1 && break; sleep 2; done
s5 ls >/dev/null || { echo "FAIL: MinIO did not become ready" >&2; exit 1; }
s5 mb "s3://${BUCKET}" >/dev/null

echo "Downloading public Bitcoin blocks 0-3 from ${ESPLORA_API}..."
# blk00000 holds heights 0-1 and is archived first; blk00001 (heights 2-3)
# joins the archive only after the first parse, as a growing node's would.
python3 - "${ESPLORA_API}" "${WORK_ROOT}/blocks/blk00000.dat" "${WORK_ROOT}/later-blocks/blk00001.dat" \
  "${WORK_ROOT}/rpc-heights.json" <<'PY'
import json, struct, sys
import urllib.request
from pathlib import Path

api, first_file, second_file, mapping_path = sys.argv[1:]
records = []
for height in range(4):
    with urllib.request.urlopen(f"{api}/block-height/{height}", timeout=60) as response:
        block_hash = response.read().decode("ascii").strip()
    with urllib.request.urlopen(f"{api}/block/{block_hash}/raw", timeout=120) as response:
        raw = response.read()
    if len(raw) < 81:
        raise SystemExit(f"public block {height} is unexpectedly short")
    records.append((block_hash, height, raw))
for destination, file_records in ((first_file, records[:2]), (second_file, records[2:])):
    with Path(destination).open("wb") as stream:
        for _, _, raw in file_records:
            stream.write(bytes.fromhex("f9beb4d9"))
            stream.write(struct.pack("<I", len(raw)))
            stream.write(raw)
Path(mapping_path).write_text(json.dumps({block_hash: height for block_hash, height, _ in records}), encoding="utf-8")
PY

cat >"${WORK_ROOT}/fake-bitcoin-cli" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "${2:-}" in
  getblockheader)
    python3 - "${FAKE_RPC_HEIGHTS:?}" "${3:?missing block hash}" <<'PY'
import json, sys
heights = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps({"height": heights[sys.argv[2]]}))
PY
    ;;
  getblockchaininfo) printf '{"blocks": 3, "pruned": false}\n' ;;
  *) echo "unsupported fixture RPC: ${2:-}" >&2; exit 2 ;;
esac
SH
chmod 0755 "${WORK_ROOT}/fake-bitcoin-cli"

echo "Archiving the fixture through bitcoin-block-archive..."
if [[ -z "${BITCOIN_BLOCK_ARCHIVE_IMAGE:-}" ]]; then
  BITCOIN_BLOCK_ARCHIVE_IMAGE="bitcoin-block-archive-e2e:${RUN_TOKEN}"
  echo "Building bitcoin-block-archive image ${BITCOIN_BLOCK_ARCHIVE_IMAGE}..."
  docker build -t "${BITCOIN_BLOCK_ARCHIVE_IMAGE}" "${ARCHIVE_PROJECT_DIR}"
  BUILT_BITCOIN_BLOCK_ARCHIVE_IMAGE=1
fi
docker image inspect "${BITCOIN_BLOCK_ARCHIVE_IMAGE}" >/dev/null 2>&1 || {
  echo "FAIL: bitcoin-block-archive image is unavailable: ${BITCOIN_BLOCK_ARCHIVE_IMAGE}" >&2
  exit 2
}
archive_blocks() {
  docker run --rm --user "$(id -u):$(id -g)" \
    -e FAKE_RPC_HEIGHTS=/fixture/rpc-heights.json \
    -e S3_ACCESS_KEY_ID="${MINIO_ROOT_USER}" \
    -e S3_SECRET_ACCESS_KEY="${MINIO_ROOT_PASSWORD}" \
    -e S3_ENDPOINT_URL="${S3_ENDPOINT_URL}" \
    -e S3_DESTINATION="${BLOCKS_URI}" \
    -e S3_PROFILE="${S3_PROFILE}" \
    -v "${WORK_ROOT}/blocks:/blocks:ro" \
    -v "${WORK_ROOT}/state:/state" \
    -v "${WORK_ROOT}/fake-bitcoin-cli:/fixture/fake-bitcoin-cli:ro" \
    -v "${WORK_ROOT}/rpc-heights.json:/fixture/rpc-heights.json:ro" \
    "${BITCOIN_BLOCK_ARCHIVE_IMAGE}" \
    --block-dir /blocks --state-dir /state \
    --bitcoin-cli /fixture/fake-bitcoin-cli --bitcoin-datadir /fixture/bitcoin \
    --keep-latest-files 0
}
archive_blocks
s5 cp "${BLOCKS_URI}/blk00000.dat.json" "${WORK_ROOT}/blk00000.dat.json" >/dev/null

export PBS_CONTAINER_NAME PBS_WORKDIR_HOST="${WORK_ROOT}" PBS_WORKDIR_CONTAINER="${WORK_ROOT}"
"${PBS_HELPER}" start
source "${PBS_ENV}"
docker cp "${WORK_ROOT}/bin/s5cmd" "${PBS_CONTAINER_NAME}:/usr/bin/s5cmd"
docker exec -u root "${PBS_CONTAINER_NAME}" chmod 0755 /usr/bin/s5cmd
if [[ -n "${PBS_BLOCKSCI_LOCAL_IMAGE}" ]]; then
  docker image inspect "${PBS_BLOCKSCI_LOCAL_IMAGE}" >/dev/null 2>&1 || { echo "FAIL: local BlockSci image is unavailable" >&2; exit 2; }
  mkdir -p "${WORK_ROOT}/pbs-images"
  docker save "${PBS_BLOCKSCI_LOCAL_IMAGE}" -o "${WORK_ROOT}/pbs-images/blocksci.tar"
  chmod 0644 "${WORK_ROOT}/pbs-images/blocksci.tar"
  PBS_IMAGE_ARGS=(--pbs-blocksci-image "docker-archive:${WORK_ROOT}/pbs-images/blocksci.tar")
else
  PBS_IMAGE_ARGS=(--pbs-blocksci-image "${BLOCKSCI_IMAGE}")
fi

export PBS_CLIENT_WORKDIR="${WORK_ROOT}" EMULATION_LOGS_DIR="${LOGS_ROOT}"

submit_blocksci() {
  local run_id="$1"
  shift
  (
    cd "${PROJECT_DIR}"
    PYTHONPATH="${PROJECT_DIR}/src:${PROJECT_DIR}/pipeline${PYTHONPATH:+:${PYTHONPATH}}" \
      timeout --foreground "${E2E_TIMEOUT}" python3 -m coinjoin_pipeline.cli pbs-from-s3 \
      --engine joinmarket --artifact-uri "${ARTIFACT_URI}" --run-id "${run_id}" \
      --s3-endpoint-url "${S3_ENDPOINT_URL}" --s3-credentials-file "${CREDENTIALS_FILE}" --s3-profile "${S3_PROFILE}" \
      --blocksciPbs "$@" --blocksci-bitcoin-blocks-uri "${BLOCKS_URI}" --blocksci-network bitcoin \
      "${PBS_IMAGE_ARGS[@]}" --pbs-ncpus 2 --pbs-mem 4gb --pbs-scratch 2gb --pbs-walltime 00:20:00
  ) 2>&1 | tee -a "${PIPELINE_OUTPUT_FILE}"
}

wait_for_stage() {
  local run_id="$1" stage="$2"
  local deadline=$((SECONDS + ${BITCOIN_BLOCK_ARCHIVE_S3_WAIT_SECONDS:-1800}))
  until s5 ls "${ARTIFACT_URI}/${run_id}/.pbs/${stage}.done" >/dev/null 2>&1; do
    if s5 ls "${ARTIFACT_URI}/${run_id}/.pbs/${stage}.failed" >/dev/null 2>&1; then
      echo "FAIL: ${stage} failed for ${run_id}" >&2
      exit 1
    fi
    if (( SECONDS >= deadline )); then echo "FAIL: timed out waiting for ${stage} of ${run_id}" >&2; exit 1; fi
    sleep 5
  done
}

fetch_cache() {
  local run_id="$1" name="$2"
  s5 cp "${ARTIFACT_URI}/${run_id}/blocksci-parse_data/manifest.json" "${WORK_ROOT}/${name}-manifest.json" >/dev/null
  s5 cp "${ARTIFACT_URI}/${run_id}/blocksci-parse_data/blocksci_data.tar.gz" "${WORK_ROOT}/${name}.tar.gz" >/dev/null
  mkdir -p "${WORK_ROOT}/${name}"
  tar -C "${WORK_ROOT}/${name}" -xzf "${WORK_ROOT}/${name}.tar.gz"
}

echo "Submitting BlockSci S3 parse for ${RUN_ID}..."
submit_blocksci "${RUN_ID}" --blocksci-workflow reusable --blocksci-task parse --blocksci-max-block 1
wait_for_stage "${RUN_ID}" blocksci-parse
fetch_cache "${RUN_ID}" blocksci-parse

echo "Growing the archive by blk00001.dat (heights 2-3)..."
cp "${WORK_ROOT}/later-blocks/blk00001.dat" "${WORK_ROOT}/blocks/blk00001.dat"
archive_blocks
s5 cp "${BLOCKS_URI}/blk00001.dat.json" "${WORK_ROOT}/blk00001.dat.json" >/dev/null

echo "Submitting incremental BlockSci update ${RUN_ID} -> ${UPDATE_RUN_ID}..."
submit_blocksci "${UPDATE_RUN_ID}" --blocksci-workflow cached --blocksci-task update \
  --blocksci-cache-source-run-id "${RUN_ID}" --blocksci-max-block 3
wait_for_stage "${UPDATE_RUN_ID}" blocksci-update
fetch_cache "${UPDATE_RUN_ID}" blocksci-update

echo "Submitting reference full parse ${FULL_RUN_ID}..."
submit_blocksci "${FULL_RUN_ID}" --blocksci-workflow reusable --blocksci-task parse --blocksci-max-block 3
wait_for_stage "${FULL_RUN_ID}" blocksci-parse
fetch_cache "${FULL_RUN_ID}" blocksci-full-parse

# The incremental index must describe the same chain as a parse from scratch.
for chain_file in block.dat tx_hashes.dat; do
  cmp -s "${WORK_ROOT}/blocksci-update/blocksci_data/parsed/chain/${chain_file}" \
    "${WORK_ROOT}/blocksci-full-parse/blocksci_data/parsed/chain/${chain_file}" || {
    echo "FAIL: incremental update and full parse differ in parsed/chain/${chain_file}" >&2
    exit 1
  }
done

python3 - "${WORK_ROOT}" <<'PY'
import json, sys
from pathlib import Path

root = Path(sys.argv[1])


def load(name):
    return json.loads((root / name).read_text(encoding="utf-8"))


first, second = load("blk00000.dat.json"), load("blk00001.dat.json")
parse = load("blocksci-parse-manifest.json")
update = load("blocksci-update-manifest.json")
full = load("blocksci-full-parse-manifest.json")
if first.get("schema_version") != 1 or first.get("file") != "blk00000.dat" or first.get("height_ranges") != [[0, 1]]:
    raise SystemExit("FAIL: blk00000.dat sidecar has wrong coverage")
if second.get("file") != "blk00001.dat" or second.get("height_ranges") != [[2, 3]]:
    raise SystemExit("FAIL: blk00001.dat sidecar has wrong coverage")
if parse.get("source_kind") != "bitcoin-blocks-s3" or parse.get("network") != "bitcoin" or parse.get("exported_max_block") != 1:
    raise SystemExit("FAIL: parsed cache does not preserve the S3 fixture provenance")
if parse.get("block_archive_last_file") != 0:
    raise SystemExit(f"FAIL: parsed cache does not record its last archive file: {parse}")
expected_update = {
    "source_kind": "bitcoin-blocks-s3",
    "exported_max_block": 3,
    "cache_operation": "incremental-update",
    "source_exported_max_block": 1,
    "block_archive_last_file": 1,
}
if any(update.get(key) != value for key, value in expected_update.items()):
    raise SystemExit(f"FAIL: updated cache manifest is wrong: {update}")
if update.get("block_archive_index_sha256") != full.get("block_archive_index_sha256"):
    raise SystemExit("FAIL: update and full parse indexed different archive files")
print("PASS: public Bitcoin fixture -> bitcoin-block-archive -> MinIO -> PBS BlockSci parse -> incremental update")
PY
