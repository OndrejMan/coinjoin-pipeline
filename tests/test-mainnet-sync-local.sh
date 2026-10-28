#!/usr/bin/env bash
# Real Core + Dumplings + archive -> local OpenPBS/Apptainer -> BlockSci,
# verified MinIO backups, pruning, incremental update and checkpoint recovery.
# regtest mines a fixture; mainnet downloads public Bitcoin P2P data near target_height.
# Existing images only. Never contacts MetaCentrum or CESNET S3.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
[[ $# -le 1 ]] || { echo 'Expected at most one test network argument.' >&2; exit 2; }
mode=${1:-regtest}
case "$mode" in
  regtest) target=1200 ;;
  mainnet) target=${MAINNET_TEST_TARGET_HEIGHT:-400000} ;;
  -h|--help)
    echo 'Usage: test-mainnet-sync-local.sh [regtest|mainnet]'
    echo 'mainnet: MAINNET_TEST_TARGET_HEIGHT=400000 (1..400000), then another 1000 blocks.'
    exit 0 ;;
  *) echo "Unknown test network: $mode" >&2; exit 2 ;;
esac
if [[ ! "$target" =~ ^[1-9][0-9]{0,5}$ ]] || (( target > 400000 )); then
  echo 'MAINNET_TEST_TARGET_HEIGHT must be an integer from 1 to 400000' >&2
  exit 2
fi
# Independently bound a broken target-height implementation before it fills the runner.
if [[ "$mode" == mainnet ]]; then
  max_data_mb=${MAINNET_TEST_MAX_DATA_MB:-262144}
  generation_timeout=${MAINNET_TEST_GENERATION_TIMEOUT:-34200}
else
  max_data_mb=${MAINNET_TEST_MAX_DATA_MB:-8192}
  generation_timeout=${MAINNET_TEST_GENERATION_TIMEOUT:-1800}
fi
for value in "$max_data_mb" "$generation_timeout"; do
  [[ "$value" =~ ^[1-9][0-9]{0,5}$ ]] || { echo 'Invalid positive test limit' >&2; exit 2; }
done
# Transport auth is always the isolated MinIO credentials below.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_DEFAULT_PROFILE AWS_REGION AWS_DEFAULT_REGION
token="$(date -u +%Y%m%dT%H%M%SZ)-$$"
work=$(mktemp -d "${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}/mainnet-sync-$mode-$token.XXXXXX")
pbs="pbs-mainnet-sync-$$"
minio="minio-mainnet-sync-$$"
seed="core-mainnet-sync-$$"
tool_container=""
core_image=${MAINNET_CORE_LOCAL_IMAGE:-bitcoin-block-archive:sync-local}
dump_image=${MAINNET_DUMPLINGS_LOCAL_IMAGE:-dumplings:sync-local}
block_image=${MAINNET_BLOCKSCI_LOCAL_IMAGE:-blocksci-complete:sync-local}
helper="$repo/tests/support/pbs/local-pbs.sh"
export PBS_CONTAINER_NAME="$pbs" PBS_WORKDIR_HOST="$work" PBS_WORKDIR_CONTAINER="$work"
export PBS_CLIENT_WORKDIR="$work" PBS_IMAGE="${PBS_IMAGE:-bitcoin-analysis/pbs-apptainer:23.06.06-1.5.1-r2}"
result="${TEST_RESULT_DIR:-$repo/emulation_logs/_test-results}/mainnet-sync-$mode"
mkdir -p "$result"
export PYTHONPATH="$repo/src:$repo/pipeline${PYTHONPATH:+:$PYTHONPATH}"
python=${MAINNET_TEST_PYTHON:-$repo/.venv/bin/python}
[[ -x "$python" ]] || { echo "Python environment missing: $python" >&2; exit 2; }
mkdir -p "$work/bin" "$work/images" "$work/data/bitcoin" "$work/runs" "$work/minio"
chmod 0755 "$work"
cp "$0" "$work/test-mainnet-sync-local.sh"
pointer=/tmp/mainnet-sync-local-result
[[ "$mode" != mainnet ]] || pointer=/tmp/mainnet-sync-mainnet-result
printf '%s\n' "$work" >"${MAINNET_LOCAL_RESULT_FILE:-$pointer}"
printf '%s\n' "$work" >"$result/working-directory.txt"
echo "Local integration evidence: $work"
cleanup() {
  status=$?
  trap - EXIT TERM INT
  set +e
  docker logs "$pbs" >"$work/pbs-container.log" 2>&1 || true
  docker exec -u pbsuser "$pbs" qstat -xf >"$work/pbs-history.txt" 2>&1 || true
  docker logs "$minio" >"$work/minio.log" 2>&1 || true
  docker logs "$seed" >"$work/seed.log" 2>&1 || true
  docker rm -f "$seed" "$pbs" "$minio" >/dev/null 2>&1 || true
  [[ -z "$tool_container" ]] || docker rm -f "$tool_container" >/dev/null 2>&1 || true
  printf '%s\n' "$status" >"$work/exit-status"
  # Upload only diagnostics in CI; raw blocks, indexes, SIFs and backups stay in work.
  "$python" "$repo/tests/support/mainnet_sync_e2e.py" diagnostics "$work" "$result" "$mode" "$status"
  echo "Kept data, index, backups and logs: $work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
for image in "$core_image" "$dump_image" "$block_image" "$PBS_IMAGE" pgsty/minio:RELEASE.2026-08-04T00-00-00Z; do
  docker image inspect "$image" >/dev/null
done
# s5cmd is a static binary; no local or remote user credentials are copied.
tool_container=$(docker create --entrypoint true "$core_image")
docker cp "$tool_container:/usr/local/bin/s5cmd" "$work/bin/s5cmd"
docker rm "$tool_container" >/dev/null
tool_container=""
chmod 0755 "$work/bin/s5cmd"
export PATH="$work/bin:$PATH"
docker run -d --name "$minio" -p 9000 -e MINIO_ROOT_USER=local-sync-user \
  -e MINIO_ROOT_PASSWORD=local-sync-test-password \
  -v "$work/minio:/data" \
  pgsty/minio:RELEASE.2026-08-04T00-00-00Z server /data >/dev/null
gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
port=$(docker port "$minio" 9000/tcp | head -n 1 | awk -F: '{print $NF}')
endpoint="http://$gateway:$port"
printf '[local]\naws_access_key_id = local-sync-user\naws_secret_access_key = local-sync-test-password\n' >"$work/credentials"
chmod 0600 "$work/credentials"
s5() { s5cmd --credentials-file "$work/credentials" --profile local --endpoint-url "$endpoint" "$@"; }
for _ in {1..40}; do s5 ls >/dev/null 2>&1 && break; sleep 1; done
s5 mb s3://local-mainnet-sync >/dev/null
export S3_ENDPOINT_URL="$endpoint" S3_PROFILE=local S3_CREDENTIALS_FILE="$work/credentials"
bash "$helper" start >"$work/pbs-start.log" 2>&1
# Install transport tools only inside this isolated local PBS container.
docker exec -u root "$pbs" bash -lc 'command -v zstd >/dev/null || dnf install -y zstd' >"$work/pbs-tools.log" 2>&1
docker cp "$work/bin/s5cmd" "$pbs:/usr/local/bin/s5cmd"
docker exec -u root "$pbs" chmod 0755 /usr/local/bin/s5cmd
for name in core dumplings blocksci; do
  case "$name" in core) image=$core_image ;; dumplings) image=$dump_image ;; blocksci) image=$block_image ;; esac
  if [[ -n "${MAINNET_LOCAL_SIF_DIR:-}" && -f "$MAINNET_LOCAL_SIF_DIR/$name.sif" ]]; then
    echo "Reusing explicitly selected local $name SIF"
    cp --reflink=auto "$MAINNET_LOCAL_SIF_DIR/$name.sif" "$work/images/$name.sif"
    continue
  fi
  deadline=$((SECONDS + 1800))
  until docker image inspect "$image" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || { echo "Image is still missing: $image" >&2; exit 1; }
    sleep 2
  done
  echo "Converting local $name image for Apptainer"
  docker save "$image" -o "$work/images/$name.tar"
  docker exec -u pbsuser "$pbs" bash -lc \
    'export APPTAINER_CACHEDIR=/scratch/mainnet-image-cache APPTAINER_TMPDIR=/scratch/mainnet-image-tmp; mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"; singularity build "$1" "docker-archive://$2"' \
    _ "$work/images/$name.sif" "$work/images/$name.tar" >"$work/convert-$name.log" 2>&1
done
seed_start() {
  local chain_args=()
  if [[ "$mode" == regtest ]]; then
    chain_args=(-regtest -fastprune=1 -fallbackfee=0.0001)
  fi
  docker run -d --name "$seed" --network none --user "$(id -u):$(id -g)" \
    -v "$work/data/bitcoin:/bitcoin" --entrypoint bitcoind "$core_image" \
    -datadir=/bitcoin "${chain_args[@]}" -server -listen=0 -networkactive=0 \
    -blocksxor=0 -prune=1 -txindex=0 >"$work/seed-container-id"
  for _ in {1..60}; do core getblockcount >/dev/null 2>&1 && return; sleep 1; done
  echo 'Fixture Core did not start' >&2; return 1
}
core() {
  local chain=main
  [[ "$mode" != regtest ]] || chain=regtest
  docker exec --user "$(id -u):$(id -g)" "$seed" bitcoin-cli -datadir=/bitcoin -chain="$chain" "$@"
}
seed_stop() {
  core stop >/dev/null
  docker wait "$seed" >/dev/null
  docker logs "$seed" >"$work/seed-$1.log" 2>&1
  docker rm "$seed" >/dev/null
}
if [[ "$mode" == regtest ]]; then
  seed_start
  core createwallet fixture >/dev/null
  address=$(core getnewaddress)
  core generatetoaddress 1200 "$address" >"$work/initial-mined-blocks.json"
  seed_stop initial
fi
source "$repo/tests/support/pbs/pbs-env.sh"
make_config() {
  "$python" "$repo/tests/support/mainnet_sync_e2e.py" config "$work" "$endpoint" "$1" "$mode" "$2"
}
run_generation() {
  run=$1
  make_config "$run" "$2"
  echo "Submitting local generation $run"
  (cd "$work" && "$python" -m coinjoin_pipeline.cli run "$work/$run.yaml") >"$work/$run-launch.log" 2>&1
  deadline=$((SECONDS + generation_timeout))
  while (( SECONDS < deadline )); do
    "$python" "$repo/tests/support/mainnet_sync_e2e.py" guard "$work" "$mode" "$max_data_mb"
    pending=0
    for marker in "$work/runs/$run/.pbs/"*.jobid; do
      job=$(cat "$marker")
      info=$(qstat -xf "$job" 2>/dev/null || true)
      if [[ "$info" =~ Exit_status[[:space:]]*=[[:space:]]*(-?[0-9]+) ]]; then
        if [[ "${BASH_REMATCH[1]}" != 0 ]]; then
          printf '%s\n' "$info" >"$work/$run-failed-job.txt"
          echo "PBS stage failed: $(basename "$marker")" >&2
          tail -n 25 "$work/data/runs/$run/"*.log >&2 || true
          return 1
        fi
      else
        pending=1
      fi
    done
    if [[ -f "$work/data/runs/$run/done.json" && "$pending" == 0 ]]; then
      echo "Completed $run (both PBS jobs exited successfully)"
      return
    fi
    sleep 2
  done
  echo "Timed out: $run" >&2; return 1
}
run_generation "local-sync-$token-1" "$target"
first_done="$work/data/runs/local-sync-$token-1/done.json"
"$python" "$repo/tests/support/mainnet_sync_e2e.py" first "$work" "$first_done" "$mode" "$target"
if [[ "$mode" == regtest ]]; then
  seed_start
  core loadwallet fixture >/dev/null
  core generatetoaddress 200 "$address" >"$work/incremental-mined-blocks.json"
  seed_stop update
  next_target=1400
else
  # Use the actual frozen height, since already received blocks can overshoot the request.
  next_target=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["height"]+1000)' "$first_done")
fi
run_generation "local-sync-$token-2" "$next_target"
s5 cp s3://local-mainnet-sync/core/manifest.json "$work/core-manifest.json" >/dev/null
if [[ $("$python" "$repo/tests/support/mainnet_sync_e2e.py" archive-required "$work" "$mode") == yes ]]; then
  s5 --json ls s3://local-mainnet-sync/blocks/blk00000.dat >"$work/archived-oldest-block.json"
else
  echo 'Bounded mainnet has no completed block files; verifying their recovery from the Core checkpoint.'
  printf '%s\n' '{"state":"pending","reason":"archiver retains the two newest block files"}' >"$work/archived-oldest-block.json"
fi
s5 ls 's3://local-mainnet-sync/*' >"$work/s3-objects.txt"
"$python" - "$work" <<'RECOVERY_SETTINGS'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
config = __import__('yaml').safe_load(next(work.glob('*-2.yaml')).read_text())
settings = dict(root=str(work/'data'), index_dir=str(work/'index'), run_id='explicit-local-recovery',
    artifact_uri=config['artifacts']['uri'], endpoint=config['artifacts']['endpoint_url'],
    credentials=str(work/'credentials'), profile='local')
(work/'recovery.json').write_text(json.dumps(settings))
RECOVERY_SETTINGS
"$python" -m coinjoin_pipeline.execution.mainnet_runtime "$work/recovery.json" restore s3://local-mainnet-sync/core/manifest.json "$work/recovered"
"$python" -m coinjoin_pipeline.execution.mainnet_runtime "$work/recovery.json" restore s3://local-mainnet-sync/index/manifest.json "$work/recovered-index"
"$python" - "$work" <<'INDEX_READER'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
config = json.loads((work/'recovered-index/config.json').read_text())
config['chainConfig']['dataDirectory'] = str(work/'recovered-index/parsed')
(work/'recovered-index-read.json').write_text(json.dumps(config))
INDEX_READER
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -e PYTHONPATH=/blocksci/.venv/lib/python3.8/site-packages:/mnt/blocksci/blockscipy \
  -v "$work:$work:ro" --entrypoint /usr/bin/python3 "$block_image" -c \
  'import blocksci,json,sys; chain=blocksci.Blockchain(sys.argv[1],0); print(json.dumps(dict(blocks=len(chain),height=chain[-1].height,hash=str(chain[-1].hash),genesis=str(chain[0].hash))))' \
  "$work/recovered-index-read.json" >"$work/recovered-index-info.json"
recovery_args=()
[[ "$mode" != regtest ]] || recovery_args=(-regtest -fastprune=1)
docker run -d --name "$seed" --network none --user "$(id -u):$(id -g)" \
  -v "$work/recovered/bitcoin:/bitcoin" --entrypoint bitcoind "$core_image" \
  -datadir=/bitcoin "${recovery_args[@]}" -server -listen=0 -networkactive=0 \
  -blocksxor=0 -prune=1 -txindex=0 -disablewallet=1 >"$work/recovered-core-container-id"
for _ in {1..60}; do core getblockcount >/dev/null 2>&1 && break; sleep 1; done
core getblockchaininfo >"$work/recovered-core-info.json"
core getindexinfo >"$work/recovered-core-indexes.json"
core getblockhash 0 >"$work/recovered-genesis.txt"
seed_stop recovery
"$python" "$repo/tests/support/mainnet_sync_e2e.py" guard "$work" "$mode" "$max_data_mb"
"$python" "$repo/tests/support/mainnet_sync_e2e.py" final "$work" "$token" "$mode" "$next_target"
