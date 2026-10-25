#!/usr/bin/env bash
# Real Core + Dumplings + archive -> local OpenPBS/Apptainer -> BlockSci,
# verified MinIO backups, pruning, incremental update and checkpoint recovery.
# Existing freshly built images only. Never contacts MetaCentrum or CESNET S3.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
token="$(date -u +%Y%m%dT%H%M%SZ)-$$"
work=$(mktemp -d "${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}/mainnet-sync-$token.XXXXXX")
pbs="pbs-mainnet-sync-$$"
minio="minio-mainnet-sync-$$"
seed="core-mainnet-sync-$$"
core_image=${MAINNET_CORE_LOCAL_IMAGE:-bitcoin-block-archive:sync-local}
dump_image=${MAINNET_DUMPLINGS_LOCAL_IMAGE:-dumplings:sync-local}
block_image=${MAINNET_BLOCKSCI_LOCAL_IMAGE:-blocksci-complete:sync-local}
helper="$repo/tests/support/pbs/local-pbs.sh"
export PBS_CONTAINER_NAME="$pbs" PBS_WORKDIR_HOST="$work" PBS_WORKDIR_CONTAINER="$work"
export PBS_CLIENT_WORKDIR="$work" PBS_IMAGE=bitcoin-analysis/pbs-apptainer:23.06.06-1.5.1-r2
mkdir -p "$work/bin" "$work/images" "$work/data/bitcoin" "$work/runs" "$work/minio"
chmod 0755 "$work"
cp "$0" "$work/test-mainnet-sync-local.sh"
printf '%s\n' "$work" >"${MAINNET_LOCAL_RESULT_FILE:-/tmp/mainnet-sync-local-result}"
echo "Local integration evidence: $work"
cleanup() {
  status=$?
  trap - EXIT
  docker logs "$pbs" >"$work/pbs-container.log" 2>&1 || true
  docker exec -u pbsuser "$pbs" qstat -xf >"$work/pbs-history.txt" 2>&1 || true
  docker logs "$minio" >"$work/minio.log" 2>&1 || true
  docker logs "$seed" >"$work/seed.log" 2>&1 || true
  docker rm -f "$seed" "$pbs" "$minio" >/dev/null 2>&1 || true
  printf '%s\n' "$status" >"$work/exit-status"
  echo "Kept data, index, backups and logs: $work"
  exit "$status"
}
trap cleanup EXIT
for image in "$core_image" "$dump_image" "$PBS_IMAGE" pgsty/minio:RELEASE.2026-08-04T00-00-00Z; do
  docker image inspect "$image" >/dev/null
done
# s5cmd is a static binary; no local or remote user credentials are copied.
tool_container=$(docker create --entrypoint true "$core_image")
docker cp "$tool_container:/usr/local/bin/s5cmd" "$work/bin/s5cmd"
docker rm "$tool_container" >/dev/null
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
  docker run -d --name "$seed" --network none --user "$(id -u):$(id -g)" \
    -v "$work/data/bitcoin:/bitcoin" --entrypoint bitcoind "$core_image" \
    -datadir=/bitcoin -regtest -server -listen=0 -networkactive=0 -fastprune=1 \
    -blocksxor=0 -prune=1 -txindex=0 -fallbackfee=0.0001 >"$work/seed-container-id"
  for _ in {1..60}; do core getblockcount >/dev/null 2>&1 && return; sleep 1; done
  echo 'Fixture Core did not start' >&2; return 1
}
core() { docker exec --user "$(id -u):$(id -g)" "$seed" bitcoin-cli -datadir=/bitcoin -regtest "$@"; }
seed_stop() {
  core stop >/dev/null
  docker wait "$seed" >/dev/null
  docker logs "$seed" >"$work/seed-$1.log" 2>&1
  docker rm "$seed" >/dev/null
}
seed_start
core createwallet fixture >/dev/null
address=$(core getnewaddress)
core generatetoaddress 1200 "$address" >"$work/initial-mined-blocks.json"
seed_stop initial
source "$repo/tests/support/pbs/pbs-env.sh"
make_config() {
  python3 - "$work" "$endpoint" "$1" <<'LOCAL_CONFIG'
import sys, yaml
from pathlib import Path
work, endpoint, run = sys.argv[1:]
value = dict(action='mainnet-sync', run_id=run, runs_root=work+'/runs',
    artifacts=dict(backend='s3', uri='s3://local-mainnet-sync/runs', endpoint_url=endpoint,
                   credentials_file=work+'/credentials', profile='local'),
    mainnet=dict(root=work+'/data', network='bitcoin_regtest', scan_start_height=1,
                 core_image=work+'/images/core.sif', dumplings_image=work+'/images/dumplings.sif',
                 dbcache_mb=128, reorg_reserve=288, poll_seconds=1,
                 blocks_uri='s3://local-mainnet-sync/blocks', dumplings_uri='s3://local-mainnet-sync/dumplings',
                 backup_uri='s3://local-mainnet-sync/core'),
    blocksci=dict(index_dir=work+'/index', index_backup_uri='s3://local-mainnet-sync/index'),
    pbs=dict(blocksci_image=work+'/images/blocksci.sif',
             producer=dict(ncpus=2, mem='4gb', scratch='2gb', scratch_type='local', walltime='00:20:00'),
             blocksci=dict(ncpus=4, mem='8gb', scratch='2gb', scratch_type='local', walltime='00:20:00')))
Path(work, run+'.yaml').write_text(yaml.safe_dump(value, sort_keys=False))
LOCAL_CONFIG
}
run_generation() {
  run=$1
  make_config "$run"
  echo "Submitting local generation $run"
  (cd "$work" && "$repo/.venv/bin/python" -m coinjoin_pipeline.cli run "$work/$run.yaml") >"$work/$run-launch.log" 2>&1
  deadline=$((SECONDS + 1200))
  while (( SECONDS < deadline )); do
    if [[ -f "$work/data/runs/$run/done.json" ]]; then
      echo "Completed $run"
      return
    fi
    for marker in "$work/runs/$run/.pbs/"*.jobid; do
      job=$(cat "$marker")
      info=$(qstat -xf "$job" 2>/dev/null || true)
      if [[ "$info" =~ Exit_status[[:space:]]*=[[:space:]]*(-?[0-9]+) ]] && [[ "${BASH_REMATCH[1]}" != 0 ]]; then
        printf '%s\n' "$info" >"$work/$run-failed-job.txt"
        echo "PBS stage failed: $(basename "$marker")" >&2
        tail -n 25 "$work/data/runs/$run/"*.log >&2 || true
        return 1
      fi
    done
    sleep 2
  done
  echo "Timed out: $run" >&2; return 1
}
run_generation "local-sync-$token-1"
first_done="$work/data/runs/local-sync-$token-1/done.json"
python3 - "$work" "$first_done" <<'FIRST_CHECK'
import json, sys
from pathlib import Path
work, done_path = Path(sys.argv[1]), Path(sys.argv[2])
done = json.loads(done_path.read_text())
assert done['height'] == 1200 and done['state'] == 'done'
files = sorted((work/'data/bitcoin/regtest/blocks').glob('blk*.dat'))
assert files and files[0].name != 'blk00000.dat', 'pruning must really remove old files'
assert not (work/'data/.sync-active').exists()
print('Verified initial height/hash, real pruning and completed checkpoint')
FIRST_CHECK
seed_start
core loadwallet fixture >/dev/null
core generatetoaddress 200 "$address" >"$work/incremental-mined-blocks.json"
seed_stop update
run_generation "local-sync-$token-2"
s5 cp s3://local-mainnet-sync/core/latest.json "$work/core-latest.json" >/dev/null
s5 cp s3://local-mainnet-sync/index/latest.json "$work/index-latest.json" >/dev/null
s5 --json head s3://local-mainnet-sync/blocks/blk00000.dat >"$work/archived-oldest-block.json"
s5 ls 's3://local-mainnet-sync/*' >"$work/s3-objects.txt"
python3 - "$work" <<'RECOVERY_SETTINGS'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
config = __import__('yaml').safe_load(next(work.glob('*-2.yaml')).read_text())
settings = dict(root=str(work/'data'), index_dir=str(work/'index'), run_id='explicit-local-recovery',
    artifact_uri=config['artifacts']['uri'], endpoint=config['artifacts']['endpoint_url'],
    credentials=str(work/'credentials'), profile='local')
(work/'recovery.json').write_text(json.dumps(settings))
RECOVERY_SETTINGS
manifest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["prefix"]+"/manifest.json")' "$work/core-latest.json")
"$repo/.venv/bin/python" -m coinjoin_pipeline.execution.mainnet_runtime "$work/recovery.json" restore "$manifest" "$work/recovered"
index_manifest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["prefix"]+"/manifest.json")' "$work/index-latest.json")
"$repo/.venv/bin/python" -m coinjoin_pipeline.execution.mainnet_runtime "$work/recovery.json" restore "$index_manifest" "$work/recovered-index"
python3 - "$work" <<'INDEX_READER'
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
  'import blocksci,json,sys; chain=blocksci.Blockchain(sys.argv[1],0); print(json.dumps(dict(blocks=len(chain),height=chain[-1].height,hash=str(chain[-1].hash))))' \
  "$work/recovered-index-read.json" >"$work/recovered-index-info.json"
docker run -d --name "$seed" --network none --user "$(id -u):$(id -g)" \
  -v "$work/recovered/bitcoin:/bitcoin" --entrypoint bitcoind "$core_image" \
  -datadir=/bitcoin -regtest -server -listen=0 -networkactive=0 -fastprune=1 \
  -blocksxor=0 -prune=1 -txindex=0 -disablewallet=1 >"$work/recovered-core-container-id"
for _ in {1..60}; do core getblockcount >/dev/null 2>&1 && break; sleep 1; done
core getblockchaininfo >"$work/recovered-core-info.json"
core getindexinfo >"$work/recovered-core-indexes.json"
seed_stop recovery
python3 - "$work" "$token" <<'FINAL_CHECK'
import json, sys
from pathlib import Path
work, token = Path(sys.argv[1]), sys.argv[2]
first = json.loads((work/'core-latest.json').read_text())
done = json.loads((work/f'data/runs/local-sync-{token}-2/done.json').read_text())
assert done['height'] == 1400 and done['state'] == 'done'
assert (work/'recovered/bitcoin/regtest/chainstate').is_dir()
assert (work/'recovered/dumplings/Scanner/Tx0Candidates.txt').is_file()
assert int((work/'recovered/dumplings/Scanner/LastProcessedBlockHeight.txt').read_text()) == 1400
assert not (work/'recovered/bitcoin/regtest/blocks/blk00000.dat').exists()
index = json.loads((work/'recovered-index-info.json').read_text())
core = json.loads((work/'recovered-core-info.json').read_text())
assert index['blocks'] == 1401 and index['height'] == 1400 and index['hash'] == done['hash']
assert core['blocks'] == 1400 and core['bestblockhash'] == done['hash']
assert core['pruned'] and not core['automatic_pruning']
assert json.loads((work/'recovered-core-indexes.json').read_text()) == {}
assert json.loads((work/'archived-oldest-block.json').read_text())['size'] > 0
print('PASS: two real local PBS generations, incremental BlockSci, verified MinIO backups, pruning, Core recovery')
FINAL_CHECK
