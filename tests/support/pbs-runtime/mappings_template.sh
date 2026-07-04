#!/usr/bin/env bash
#PBS -N coinjoin_mappings
#PBS -l select=1:ncpus={ncpus}:mem={mem}:scratch_local={scratch}
#PBS -l walltime={walltime}
#PBS -j oe
set -euo pipefail
cd "$PBS_O_WORKDIR"
test -n "$SCRATCHDIR" || {{ echo "SCRATCHDIR is not set"; exit 1; }}
export TMPDIR="$SCRATCHDIR"
export SINGULARITY_CACHEDIR="$SCRATCHDIR"
export SINGULARITY_TMPDIR="$SCRATCHDIR"
export SINGULARITY_LOCALCACHEDIR="$SCRATCHDIR"
RUN_DIR="{run_dir}"
OUT="$RUN_DIR/coinjoin-mappings_data"
mkdir -p "$RUN_DIR/.pbs" "$OUT"
rm -f "$RUN_DIR/.pbs/coinjoin-mappings.done" "$RUN_DIR/.pbs/coinjoin-mappings.failed"
trap 'echo failed > "$RUN_DIR/.pbs/coinjoin-mappings.failed"' ERR
INPUT="$RUN_DIR/coinjoin-analysis_data/coinjoin_tx_info.json"
ENUMERATOR_SIF="$SCRATCHDIR/coinjoin-mappings-enumerator.sif"
SAKE_SIF="$SCRATCHDIR/coinjoin-mappings-sake.sif"
case "{enumerator_image}" in
  docker://*|oras://*|library://*|http://*|https://*) singularity pull --force "$ENUMERATOR_SIF" "{enumerator_image}" ;;
  *) test -f "{enumerator_image}"; cp "{enumerator_image}" "$ENUMERATOR_SIF" ;;
esac
case "{sake_image}" in
  docker://*|oras://*|library://*|http://*|https://*) singularity pull --force "$SAKE_SIF" "{sake_image}" ;;
  *) test -f "{sake_image}"; cp "{sake_image}" "$SAKE_SIF" ;;
esac
ENUMERATOR_DIGEST="sha256:$(sha256sum "$ENUMERATOR_SIF" | awk '{{print $1}}')"
SAKE_DIGEST="sha256:$(sha256sum "$SAKE_SIF" | awk '{{print $1}}')"
singularity exec --bind "$RUN_DIR:$RUN_DIR:rw" "$ENUMERATOR_SIF" python3 /app/run.py \
  "$INPUT" --output "$OUT/enumerator.json" --mining-fee-rate {mining_fee_rate} \
  --coordination-fee-rate {coordination_fee_rate} --max-decomposition-fee {max_decomposition_fee} \
  --mode {mode} --timeout {timeout} --retry-timeout {retry_timeout}
singularity exec --bind "$RUN_DIR:$RUN_DIR:rw" "$SAKE_SIF" dotnet /app/Sake.dll \
  --input "$INPUT" --output "$OUT/sake.json" --seed {sake_seed}
singularity exec --bind "$RUN_DIR:$RUN_DIR:rw" "$ENUMERATOR_SIF" python3 -c \
  'import json,sys; p,ed,sd=sys.argv[1:]; e=json.load(open(p+"/enumerator.json")); s=json.load(open(p+"/sake.json")); status="partial" if e["summary"]["timed_out"] else "complete"; json.dump({{"schema_version":"1.0","status":status,"provenance":{{"enumerator_image":"{enumerator_image}","sake_image":"{sake_image}","enumerator_image_digest":ed,"sake_image_digest":sd}},"enumerator":e,"sake":s}},open(p+"/coinjoin_mappings.json","w"),indent=2,sort_keys=True)' "$OUT" "$ENUMERATOR_DIGEST" "$SAKE_DIGEST"
echo done > "$RUN_DIR/.pbs/coinjoin-mappings.done"
