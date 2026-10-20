test -n "${SCRATCHDIR:-}" || { echo "SCRATCHDIR is not set" >&2; exit 1; }
RUNS_ROOT="$SCRATCHDIR/coinjoin-run"
RUN_WORK="$RUNS_ROOT/$RUN_ID"
FAILED_MARKER="$RUN_WORK/.pbs/$STAGE.failed"
DONE_MARKER="$RUN_WORK/.pbs/$STAGE.done"
# Filled only for a persistent index, which the job binds at its own path.
INDEX_BINDS=()
mkdir -p "$RUN_WORK/.pbs" "$RUN_WORK/logs"
# s5cmd with the stage's credentials only: no AWS_* variable of the node may leak in.
s5() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION \
    s5cmd --credentials-file "$S3_CREDENTIALS_FILE" --profile "$S3_PROFILE" --endpoint-url "$S3_ENDPOINT_URL" "$@"
}
require_file() {
  test -f "$1" || { echo "$2" >&2; exit 1; }
}
# The highest block_<height>.json the emulator exported, which bounds the parser.
emulator_max_block() {
  find "$RUN_WORK/coinjoin_emulator_data/data/btc-node" -maxdepth 1 -type f -name 'block_*.json' -printf '%f\n' \
    | sed -nE 's/^block_([0-9]+)\.json$/\1/p' | sort -n | tail -n 1
}
publish_done() {
  s5 cp "$DONE_MARKER" "$ARTIFACT_URI/$RUN_ID/.pbs/$STAGE.done"
}
publish_failed() {
  s5 cp "$FAILED_MARKER" "$ARTIFACT_URI/$RUN_ID/.pbs/$STAGE.failed"
}
# Templates with outputs to publish even on failure redefine this.
stage_finalize() {
  :
}
# The BlockSci cache archive is zstd-compressed; fail inside the trap before any download.
require_zstd() {
  command -v zstd >/dev/null 2>&1 || { echo "zstd is required to pack or unpack the BlockSci cache" >&2; exit 1; }
}
# An index copied from another location still names its old parsed directory.
canonicalize_index() {
  local parsed="/runs/emulation/logs/$RUN_ID/blocksci_data/parsed" config="$RUN_WORK/blocksci_data/config.json"
  sed -i -E 's#("dataDirectory"[[:space:]]*:[[:space:]]*)"[^"]*"#\1"'"$parsed"'"#' "$config"
  grep -Fq "$parsed" "$config" || { echo "Could not canonicalize BlockSci dataDirectory" >&2; exit 1; }
}
