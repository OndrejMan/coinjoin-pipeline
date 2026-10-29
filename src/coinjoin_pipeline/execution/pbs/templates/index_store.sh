# Persistent BlockSci index shared by PBS jobs (scenario-lab/mainnet-analysis-plan-v2.md §2.6).
#
# $INDEX_DIR is the working copy (multijob scratch or /storage): parsed/, config.json,
# manifest.json and files.txt (every other file with its size). It is complete when it
# has no .writing marker and its files match files.txt.
#
# $INDEX_BACKUP holds one backup: blocksci_index.tar.zst and manifest.json, written last.
# A new backup deletes the old one first, so S3 never holds two copies; until its
# manifest exists there is no valid backup.
#
# Lock: one writer or many readers, as directories under $INDEX_DIR/.lock. Entries are
# built aside and renamed into place, which is atomic on NFS and BeeGFS without flock.
# Each holds the PBS job ID and an expiry past the job's walltime, so an entry left by
# a killed job is cleared instead of blocking the index forever.
#
# Needs INDEX_DIR, INDEX_BACKUP (may be empty), INDEX_LOCK_EXPIRES (epoch seconds),
# INDEX_LOCK_WAIT and INDEX_LOCK_POLL (seconds); for the backup also RUN_WORK and the
# s5 function of the stage header.

INDEX_LOCK="$INDEX_DIR/.lock"
INDEX_LOCK_ID="${PBS_JOBID:-pid-$$}"
INDEX_LOCK_HELD=""

index_lock_create() {
  local staging="$INDEX_LOCK/.new-$INDEX_LOCK_ID-$RANDOM"
  mkdir -p "$staging" || return 1
  printf '%s\n' "$INDEX_LOCK_ID" >"$staging/job"
  printf '%s\n' "$INDEX_LOCK_EXPIRES" >"$staging/expires"
  if mv -T "$staging" "$1" 2>/dev/null; then
    return 0
  fi
  rm -rf "$staging"
  return 1
}

index_lock_clear_expired() {
  local entry expires now
  now=$(date +%s)
  for entry in "$INDEX_LOCK/writer" "$INDEX_LOCK"/readers/*; do
    [ -d "$entry" ] || continue
    expires=$(cat "$entry/expires" 2>/dev/null || echo 0)
    if [ "$now" -ge "$expires" ]; then
      echo "[index] clearing expired lock $(basename "$entry") of job $(cat "$entry/job" 2>/dev/null || echo unknown)"
      rm -rf "$entry"
    fi
  done
}

index_lock_shared() {
  local deadline=$(($(date +%s) + INDEX_LOCK_WAIT))
  mkdir -p "$INDEX_LOCK/readers"
  while :; do
    index_lock_clear_expired
    if [ ! -d "$INDEX_LOCK/writer" ] && index_lock_create "$INDEX_LOCK/readers/$INDEX_LOCK_ID"; then
      # A writer that arrived in between waits for readers; step back so it can proceed.
      if [ ! -d "$INDEX_LOCK/writer" ]; then
        INDEX_LOCK_HELD="$INDEX_LOCK/readers/$INDEX_LOCK_ID"
        return 0
      fi
      rm -rf "$INDEX_LOCK/readers/$INDEX_LOCK_ID"
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[index] $INDEX_DIR is being updated; gave up after ${INDEX_LOCK_WAIT}s" >&2
      return 1
    fi
    sleep "$INDEX_LOCK_POLL"
  done
}

index_lock_exclusive() {
  local deadline=$(($(date +%s) + INDEX_LOCK_WAIT))
  mkdir -p "$INDEX_LOCK/readers"
  until index_lock_create "$INDEX_LOCK/writer"; do
    index_lock_clear_expired
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[index] another job writes $INDEX_DIR; gave up after ${INDEX_LOCK_WAIT}s" >&2
      return 1
    fi
    sleep "$INDEX_LOCK_POLL"
  done
  INDEX_LOCK_HELD="$INDEX_LOCK/writer"
  # New readers now step back; wait for the ones already reading.
  while [ -n "$(ls -A "$INDEX_LOCK/readers" 2>/dev/null)" ]; do
    index_lock_clear_expired
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "[index] readers still hold $INDEX_DIR after ${INDEX_LOCK_WAIT}s" >&2
      index_unlock
      return 1
    fi
    sleep "$INDEX_LOCK_POLL"
  done
}

index_unlock() {
  if [ -n "$INDEX_LOCK_HELD" ]; then
    rm -rf "$INDEX_LOCK_HELD"
    INDEX_LOCK_HELD=""
  fi
}

index_files() {
  (cd "$1" && find . \( -path ./.lock -o -path ./.writing \) -prune -o -type f \
    ! -name files.txt ! -name manifest.json ! -name '*.tmp' -printf '%P %s\n' | LC_ALL=C sort)
}

index_complete() {
  [ -f "$1/manifest.json" ] && [ -f "$1/files.txt" ] && [ ! -e "$1/.writing" ] \
    && index_files "$1" | cmp -s - "$1/files.txt"
}

index_height() {
  sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$1/manifest.json" | head -n 1
}

index_backup_available() {
  [ -n "$INDEX_BACKUP" ] && s5 ls "$INDEX_BACKUP/manifest.json" >/dev/null 2>&1
}

# Replace the S3 backup with the working copy. Run under at least the shared lock,
# so no writer changes the index meanwhile. Only the object size is checked after
# the upload; restore verifies the SHA-256.
index_backup() {
  local archive="$INDEX_BACKUP/blocksci_index.tar.zst"
  echo "[index] replacing the S3 backup in $INDEX_BACKUP (height $(index_height "$INDEX_DIR"))"
  s5 rm "$INDEX_BACKUP/manifest.json" >/dev/null 2>&1 || :
  s5 rm "$archive" "$archive.sha256" >/dev/null 2>&1 || :
  index_archive_upload "$INDEX_DIR" "$archive" "${INDEX_ZSTD_THREADS:-0}" config.json files.txt manifest.json parsed
  s5 --json ls "$archive" >"$RUN_WORK/index-backup-size.json"
  python3 - "$RUN_WORK/index-backup-size.json" "$archive" "$ARCHIVE_SIZE" <<'INDEX_SIZE'
import json, sys
# ls matches a prefix: the archive's .sha256 is listed beside it.
with open(sys.argv[1], encoding='utf-8') as handle:
    listed = [json.loads(line) for line in handle if line.strip()]
value = next((item for item in listed if item.get('key') == sys.argv[2]), {})
size = value.get('size')
if type(size) is not int or size != int(sys.argv[3]):
    raise ValueError('Index backup S3 size/key mismatch')
INDEX_SIZE
  python3 - "$INDEX_DIR/manifest.json" "$archive" "$ARCHIVE_SHA256" "$ARCHIVE_SIZE" >"$RUN_WORK/index-backup.json" <<'INDEX_MANIFEST'
import json, sys
from datetime import datetime, timezone
with open(sys.argv[1], encoding='utf-8') as handle:
    manifest = json.load(handle)
manifest.update(archive_uri=sys.argv[2], archive_sha256=sys.argv[3], archive_size=int(sys.argv[4]),
                upload_validation='size-only', uploaded_at=datetime.now(timezone.utc).isoformat())
print(json.dumps(manifest, indent=2, sort_keys=True))
INDEX_MANIFEST
  s5 cp "$RUN_WORK/index-backup.json" "$INDEX_BACKUP/manifest.json" >/dev/null
  echo "[index] S3 backup complete"
}

# Under the writer lock: replace an incomplete working copy with the S3 backup.
index_restore() {
  local archive expected
  s5 cat "$INDEX_BACKUP/manifest.json" >"$RUN_WORK/index-backup.json"
  python3 - "$RUN_WORK/index-backup.json" "$INDEX_BACKUP" >"$RUN_WORK/index-pointer.txt" <<'INDEX_POINTER'
import json, re, sys
value = json.load(open(sys.argv[1]))
archive, checksum = value['archive_uri'], value['archive_sha256']
if archive != sys.argv[2].rstrip('/') + '/blocksci_index.tar.zst' or not re.fullmatch(r'[0-9a-f]{64}', checksum):
    raise ValueError('Invalid index backup manifest')
print(archive)
print(checksum)
INDEX_POINTER
  archive=$(head -n 1 "$RUN_WORK/index-pointer.txt")
  expected=$(tail -n 1 "$RUN_WORK/index-pointer.txt")
  echo "[index] restoring the working copy from $archive"
  mkdir -p "$INDEX_DIR"
  touch "$INDEX_DIR/.writing"
  rm -rf "$INDEX_DIR/parsed" "$INDEX_DIR/config.json" "$INDEX_DIR/files.txt" "$INDEX_DIR/manifest.json"
  index_archive_restore "$archive" "$INDEX_DIR" "$expected"
  rm -f "$INDEX_DIR/.writing"
  index_complete "$INDEX_DIR" || { echo "[index] the restored copy does not match its file list" >&2; return 1; }
}

# Under the writer lock: leave $INDEX_DIR complete, from the S3 backup if needed.
index_reconcile() {
  if index_complete "$INDEX_DIR"; then
    return 0
  fi
  index_backup_available || { echo "[index] working copy $INDEX_DIR is incomplete and there is no S3 backup; parse again" >&2; return 1; }
  index_restore
}

# Record a finished parse or update: config, manifest and file list.
index_publish() {
  local manifest="$1"
  cp "$RUN_WORK/blocksci_data/config.json" "$INDEX_DIR/config.json"
  index_files "$INDEX_DIR" >"$INDEX_DIR/files.txt.tmp"
  mv -f "$INDEX_DIR/files.txt.tmp" "$INDEX_DIR/files.txt"
  cp "$manifest" "$INDEX_DIR/manifest.json.tmp"
  mv -f "$INDEX_DIR/manifest.json.tmp" "$INDEX_DIR/manifest.json"
  rm -f "$INDEX_DIR/.writing"
}

# The run-local config names the shared parsed/ through a symlink that the job binds
# at the same absolute path, so the index never stores a run-specific path.
index_link_into_run() {
  mkdir -p "$RUN_WORK/blocksci_data" "$INDEX_DIR/parsed"
  ln -sfn "$INDEX_DIR/parsed" "$RUN_WORK/blocksci_data/parsed"
}

# MetaCentrum's /scratch/multijob-* aliases point outside /scratch, which the node binds
# into every container: the alias alone resolves to nothing there and the bind is lost.
# Bind the target too, so the run's parsed link resolves inside the container as on the host.
index_bind() {
  local real
  real=$(cd "$INDEX_DIR" && pwd -P)
  INDEX_BINDS=(--bind "$real:$real:$1")
  [ "$real" = "$INDEX_DIR" ] || INDEX_BINDS+=(--bind "$real:$INDEX_DIR:$1")
}
