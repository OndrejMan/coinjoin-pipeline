# Persistent BlockSci index shared by PBS jobs (scenario-lab/mainnet-analysis-plan-v2.md §2.6).
#
# $INDEX_DIR is the working copy (multijob scratch or /storage): parsed/, config.json,
# manifest.json and files.txt (every other file with its size). It is complete when it
# has no .writing marker and its files match files.txt.
#
# $INDEX_BACKUP, when set, is an S3 prefix holding one archive of a complete copy:
# blocksci_index.tar.zst, its .sha256 and manifest.json. A backup first deletes the old
# one (manifest first) and writes the manifest last, so S3 never holds two copies and a
# prefix without a manifest is no backup. While it is written there is no backup.
#
# Lock: one writer or many readers, as directories under $INDEX_DIR/.lock. Entries are
# built aside and renamed into place, which is atomic on NFS and BeeGFS without flock.
# Each holds the PBS job ID and an expiry past the job's walltime, so an entry left by
# a killed job is cleared instead of blocking the index forever.
#
# Needs INDEX_DIR, INDEX_BACKUP (may be empty), INDEX_LOCK_EXPIRES (epoch seconds),
# INDEX_LOCK_WAIT and INDEX_LOCK_POLL (seconds); for the backup also RUN_WORK and the
# S3_CREDENTIALS_FILE, S3_PROFILE and S3_ENDPOINT_URL of the stage header.

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

index_s5() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_PROFILE \
    s5cmd --credentials-file "$S3_CREDENTIALS_FILE" --profile "$S3_PROFILE" \
    --endpoint-url "$S3_ENDPOINT_URL" "$@"
}

index_backup_available() {
  [ -n "$INDEX_BACKUP" ] && index_s5 ls "$INDEX_BACKUP/manifest.json" >/dev/null 2>&1
}

# Archive the working copy into $INDEX_BACKUP, replacing the old backup. Run under at
# least the shared lock, so no writer changes the index meanwhile.
index_backup() {
  local archive="$INDEX_BACKUP/blocksci_index.tar.zst" fifo="$RUN_WORK/index-backup.fifo"
  local sha="$RUN_WORK/index-backup.sha256"
  echo "[index] replacing the S3 backup in $INDEX_BACKUP (height $(index_height "$INDEX_DIR"))"
  index_s5 rm "$INDEX_BACKUP/manifest.json" >/dev/null 2>&1 || true
  index_s5 rm "$archive" "$archive.sha256" >/dev/null 2>&1 || true
  rm -f "$fifo"
  mkfifo "$fifo"
  sha256sum <"$fifo" | cut -d ' ' -f 1 >"$sha" &
  if index_s5 pipe --help >/dev/null 2>&1; then
    tar -C "$INDEX_DIR" -cf - config.json files.txt manifest.json parsed \
      | zstd -3 -T"${INDEX_ZSTD_THREADS:-0}" | tee "$fifo" | index_s5 pipe "$archive" >/dev/null
  else
    # s5cmd before 2.2 cannot read standard input; the archive passes through scratch.
    tar -C "$INDEX_DIR" -cf - config.json files.txt manifest.json parsed \
      | zstd -3 -T"${INDEX_ZSTD_THREADS:-0}" | tee "$fifo" >"$RUN_WORK/blocksci_index.tar.zst"
    index_s5 cp "$RUN_WORK/blocksci_index.tar.zst" "$archive" >/dev/null
    rm -f "$RUN_WORK/blocksci_index.tar.zst"
  fi
  wait
  rm -f "$fifo"
  printf '%s  blocksci_index.tar.zst\n' "$(cat "$sha")" >"$sha.file"
  index_s5 cp "$sha.file" "$archive.sha256" >/dev/null
  # Written last: until here the prefix is no backup.
  index_s5 cp "$INDEX_DIR/manifest.json" "$INDEX_BACKUP/manifest.json" >/dev/null
  echo "[index] S3 backup complete"
}

# Under the writer lock: replace an incomplete working copy with the S3 backup.
index_restore() {
  local archive="$INDEX_BACKUP/blocksci_index.tar.zst" fifo="$RUN_WORK/index-restore.fifo"
  local sha="$RUN_WORK/index-restore.sha256" expected
  echo "[index] restoring the working copy from $INDEX_BACKUP (height $(index_s5 cat "$INDEX_BACKUP/manifest.json" \
    | sed -nE 's/.*"exported_max_block"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' | head -n 1))"
  mkdir -p "$INDEX_DIR"
  touch "$INDEX_DIR/.writing"
  rm -rf "$INDEX_DIR/parsed" "$INDEX_DIR/config.json" "$INDEX_DIR/files.txt" "$INDEX_DIR/manifest.json"
  rm -f "$fifo"
  mkfifo "$fifo"
  sha256sum <"$fifo" | cut -d ' ' -f 1 >"$sha" &
  index_s5 cat "$archive" | tee "$fifo" | zstd -d | tar -C "$INDEX_DIR" -xf -
  wait
  rm -f "$fifo"
  expected="$(index_s5 cat "$archive.sha256" | cut -d ' ' -f 1)"
  [ "$(cat "$sha")" = "$expected" ] || { echo "[index] the S3 backup does not match its SHA-256" >&2; return 1; }
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
