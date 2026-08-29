#!/usr/bin/env bash
# Public Bitcoin fixture -> bitcoin-block-archive Docker image -> MinIO ->
# PBS/Apptainer BlockSci parse. Only mainnet genesis and height 1 are
# downloaded, so this is a real Core block-file archive without a full chain.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ARCHIVE_PROJECT_DIR="${BITCOIN_BLOCK_ARCHIVE_DIR:-${PROJECT_DIR}/../bitcoin-block-archive}"
PBS_SUPPORT_ROOT="${PBS_SUPPORT_ROOT:-${SCRIPT_DIR}/support/pbs}"
PBS_HELPER="${PBS_HELPER:-${PBS_SUPPORT_ROOT}/local-pbs.sh}"
PBS_ENV="${PBS_ENV:-${PBS_SUPPORT_ROOT}/pbs-env.sh}"

for command in docker python3 timeout; do
  command -v "${command}" >/dev/null 2>&1 || { echo "FAIL: missing ${command}" >&2; exit 2; }
done
docker info >/dev/null 2>&1 || { echo "FAIL: Docker is unavailable" >&2; exit 2; }
[[ -d "${ARCHIVE_PROJECT_DIR}/src/bitcoin_block_archive" ]] || {
  echo "FAIL: bitcoin-block-archive source is unavailable: ${ARCHIVE_PROJECT_DIR}" >&2; exit 2;
}
[[ -x "${PBS_HELPER}" && -f "${PBS_ENV}" ]] || {
  echo "FAIL: local PBS support is unavailable" >&2; exit 2;
}

RUN_TOKEN="$(TZ=Europe/Prague date +%Y%m%dT%H%M%S%Z)-$$-${RANDOM}"
RESOURCE_ID="${GITHUB_RUN_ID:-$$}"
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}"
[[ -d "${STORAGE_BASE}" && -w "${STORAGE_BASE}" ]] || {
  echo "FAIL: writable /storage root is required: ${STORAGE_BASE}" >&2; exit 2;
}
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
    find "${LOGS_ROOT}" -type f -name '*.pbs.log' -print -exec tail -n 200 {} \; 2>/dev/null || true
  } >"${DIAGNOSTICS_FILE}" 2>&1
  cat "${DIAGNOSTICS_FILE}" >&2
}

cleanup() {
  local status=$?
  trap - EXIT
  (( status == 0 )) || dump_diagnostics || true
  mkdir -p "${RESULT_DIR}"
  for artifact in archive-manifest.json blocksci-parse-manifest.json pipeline-output.log diagnostics.txt; do
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

mkdir -p "${WORK_ROOT}/bin" "${WORK_ROOT}/blocks" "${WORK_ROOT}/state" "${LOGS_ROOT}"
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
  -p 9000 "${MINIO_IMAGE:-minio/minio:latest}" server /data >/dev/null
MINIO_PORT="$(docker port "${MINIO_CONTAINER_NAME}" 9000/tcp | head -n 1 | awk -F: '{print $NF}')"
S3_ENDPOINT_URL="http://${GATEWAY}:${MINIO_PORT}"
printf '[%s]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
  "${S3_PROFILE}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" >"${CREDENTIALS_FILE}"
for _ in $(seq 1 60); do s5 ls >/dev/null 2>&1 && break; sleep 2; done
s5 ls >/dev/null || { echo "FAIL: MinIO did not become ready" >&2; exit 1; }
s5 mb "s3://${BUCKET}" >/dev/null

echo "Downloading public Bitcoin blocks 0 and 1 from ${ESPLORA_API}..."
python3 - "${ESPLORA_API}" "${WORK_ROOT}/blocks/blk00000.dat" "${WORK_ROOT}/rpc-heights.json" <<'PY'
import json, struct, sys
import urllib.request
from pathlib import Path

api, destination, mapping_path = sys.argv[1:]
records = []
for height in (0, 1):
    with urllib.request.urlopen(f"{api}/block-height/{height}", timeout=60) as response:
        block_hash = response.read().decode("ascii").strip()
    with urllib.request.urlopen(f"{api}/block/{block_hash}/raw", timeout=120) as response:
        raw = response.read()
    if len(raw) < 81:
        raise SystemExit(f"public block {height} is unexpectedly short")
    records.append((block_hash, height, raw))
with Path(destination).open("wb") as stream:
    for _, _, raw in records:
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
  getblockchaininfo) printf '{"blocks": 1}\n' ;;
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
  --keep-latest-files 0 --no-stop-on-error
s5 cp "${BLOCKS_URI}/archive-manifest.json" "${WORK_ROOT}/archive-manifest.json" >/dev/null

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
echo "Submitting BlockSci S3 parse for ${RUN_ID}..."
(
  cd "${PROJECT_DIR}"
  PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    timeout --foreground "${E2E_TIMEOUT}" python3 -m coinjoin_pipeline.cli pbs-from-s3 \
    --engine joinmarket --artifact-uri "${ARTIFACT_URI}" --run-id "${RUN_ID}" \
    --s3-endpoint-url "${S3_ENDPOINT_URL}" --s3-credentials-file "${CREDENTIALS_FILE}" --s3-profile "${S3_PROFILE}" \
    --blocksciPbs --blocksci-workflow reusable --blocksci-task parse \
    --blocksci-bitcoin-blocks-uri "${BLOCKS_URI}" --blocksci-network bitcoin --blocksci-max-block 1 \
    "${PBS_IMAGE_ARGS[@]}" --pbs-ncpus 2 --pbs-mem 4gb --pbs-scratch 2gb --pbs-walltime 00:20:00
) 2>&1 | tee "${PIPELINE_OUTPUT_FILE}"

deadline=$((SECONDS + ${BITCOIN_BLOCK_ARCHIVE_S3_WAIT_SECONDS:-1800}))
until s5 ls "${ARTIFACT_URI}/${RUN_ID}/.pbs/blocksci-parse.done" >/dev/null 2>&1; do
  if s5 ls "${ARTIFACT_URI}/${RUN_ID}/.pbs/blocksci-parse.failed" >/dev/null 2>&1; then echo "FAIL: BlockSci parse failed" >&2; exit 1; fi
  if (( SECONDS >= deadline )); then echo "FAIL: timed out waiting for BlockSci parse" >&2; exit 1; fi
  sleep 5
done
s5 cp "${ARTIFACT_URI}/${RUN_ID}/blocksci-parse_data/manifest.json" "${WORK_ROOT}/blocksci-parse-manifest.json" >/dev/null
python3 - "${WORK_ROOT}/archive-manifest.json" "${WORK_ROOT}/blocksci-parse-manifest.json" <<'PY'
import json, sys
archive, cache = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
if archive.get("schema_version") != 1 or archive.get("archived_max_height") != 1:
    raise SystemExit("FAIL: archive manifest has wrong coverage")
if [entry.get("file") for entry in archive.get("block_files", [])] != ["blk00000.dat"]:
    raise SystemExit("FAIL: archive manifest has wrong block inventory")
if cache.get("source_kind") != "bitcoin-blocks-s3" or cache.get("network") != "bitcoin" or cache.get("exported_max_block") != 1:
    raise SystemExit("FAIL: parsed cache does not preserve the S3 fixture provenance")
print("PASS: public Bitcoin fixture -> bitcoin-block-archive -> MinIO -> PBS BlockSci parse")
PY
