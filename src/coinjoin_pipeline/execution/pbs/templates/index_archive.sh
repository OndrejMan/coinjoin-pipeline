# Shared S3 archive transport for run-local indexes and the persistent index backup.
# Callers publish their manifest last, after both the archive and checksum exist.
index_archive_upload() {
  local root="$1" archive="$2" threads="$3"
  shift 3
  local work fifo size_fifo hash_pid size_pid status=0
  work="$(mktemp -d "$RUN_WORK/index-archive.XXXXXX")"
  fifo="$work/hash.fifo"
  size_fifo="$work/size.fifo"
  mkfifo "$fifo" "$size_fifo"
  sha256sum <"$fifo" | cut -d ' ' -f 1 >"$work/sha256" &
  hash_pid=$!
  wc -c <"$size_fifo" >"$work/size" &
  size_pid=$!
  if s5 pipe --help >/dev/null 2>&1; then
    tar -C "$root" -cf - "$@" | zstd -3 -T"$threads" | tee "$fifo" "$size_fifo" | s5 pipe "$archive" >/dev/null || status=$?
  else
    # Keep support for s5cmd without stdin uploads; only this branch uses archive space.
    tar -C "$root" -cf - "$@" | zstd -3 -T"$threads" | tee "$fifo" "$size_fifo" >"$work/archive.tar.zst" || status=$?
    if [ "$status" -eq 0 ]; then
      s5 cp "$work/archive.tar.zst" "$archive" >/dev/null || status=$?
    fi
  fi
  wait "$hash_pid" || status=$?
  wait "$size_pid" || status=$?
  if [ "$status" -eq 0 ]; then
    ARCHIVE_SHA256="$(cat "$work/sha256")"
    ARCHIVE_SIZE="$(tr -d '[:space:]' <"$work/size")"
    printf '%s  %s\n' "$ARCHIVE_SHA256" "${archive##*/}" >"$work/archive.sha256"
    s5 cp "$work/archive.sha256" "$archive.sha256" >/dev/null || status=$?
  fi
  rm -rf "$work"
  return "$status"
}

# Extract while hashing, but let callers expose the index only after this succeeds.
index_archive_restore() {
  local archive="$1" destination="$2" expected="$3"
  local work fifo hash_pid actual status=0
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { echo "Invalid archive SHA-256" >&2; return 1; }
  work="$(mktemp -d "$RUN_WORK/index-archive.XXXXXX")"
  fifo="$work/hash.fifo"
  mkfifo "$fifo"
  sha256sum <"$fifo" | cut -d ' ' -f 1 >"$work/sha256" &
  hash_pid=$!
  s5 cat "$archive" | tee "$fifo" | zstd -d | tar -C "$destination" -xf - || status=$?
  wait "$hash_pid" || status=$?
  actual="$(cat "$work/sha256")"
  rm -rf "$work"
  [ "$status" -eq 0 ] || return "$status"
  [ "$actual" = "$expected" ] || { echo "The S3 archive does not match its SHA-256" >&2; return 1; }
}
