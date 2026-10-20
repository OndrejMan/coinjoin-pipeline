# Runs inside the trap, so a missing credential or s5cmd still publishes the failure.
test -r "$S3_CREDENTIALS_FILE" || { echo "S3 credentials file is not readable: $S3_CREDENTIALS_FILE" >&2; exit 1; }
command -v s5cmd >/dev/null || { echo "s5cmd is required" >&2; exit 1; }
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"
