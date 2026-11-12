#PBS -o {run_dir}/logs/{stage}.pbs.log
set -euo pipefail
cd "$PBS_O_WORKDIR"
RUN_DIR="{run_dir}"
mkdir -p "$RUN_DIR/.pbs" "$RUN_DIR/logs"
rm -f "$RUN_DIR/.pbs/{stage}.done" "$RUN_DIR/.pbs/{stage}.failed"
on_exit() {{
  status=$?
  trap - EXIT TERM
  if [ "$status" -eq 0 ]; then
    echo done > "$RUN_DIR/.pbs/{stage}.done"
  else
    echo failed > "$RUN_DIR/.pbs/{stage}.failed"
  fi
  exit "$status"
}}
trap on_exit EXIT
trap 'exit 143' TERM
test -n "$SCRATCHDIR" || {{ echo "SCRATCHDIR is not set"; exit 1; }}
export TMPDIR="$SCRATCHDIR" SINGULARITY_CACHEDIR="$SCRATCHDIR" SINGULARITY_TMPDIR="$SCRATCHDIR" SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"
