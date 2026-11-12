#!/usr/bin/env bash
# CoinJoin Statistics over one mainnet Dumplings snapshot: the CRoCS daily workflow of
# coinjoin-analysis (scripts/process_ww2.sh, process_ww1.sh, process_jm.sh, visualize_*.sh) and
# the file selection of backup_and_montage.sh. Online steps are left out: fee rates and false
# positives come from the image's data/ directory instead of mempool.space.
# Usage: coinjoin_statistics.sh PROTOCOL... (wasabi2, wasabi1, joinmarket)
set -euo pipefail

ROOT="${STATISTICS_ROOT:-/work}"
DATA="${COINJOIN_ANALYSIS_DATA:-/opt/coinjoin-analysis/data}"
# The --target-path of parse_dumplings; it writes summary.log and operation.txt into its parent, $ROOT.
WORK="$ROOT/.statistics"
SCANNER="$WORK/Scanner"
# Data source of the Scenario Lab site build, the counterpart of CRoCS's nightly/ directory.
SITE="$ROOT/site"

selected() {
  local protocol
  for protocol in "${PROTOCOLS[@]}"; do
    [ "$protocol" = "$1" ] && return 0
  done
  return 1
}

# Every parse_dumplings call of the CRoCS scripts; STATISTICS_ENV_VARS adds switches to each of them.
parse() {
  local cjtype="$1" action="$2" env_vars="${3:-}"
  local argv=(python -m cj_process.parse_dumplings --cjtype "$cjtype")
  [ -n "$action" ] && argv+=(--action "$action")
  env_vars="${env_vars}${STATISTICS_ENV_VARS:+${env_vars:+;}$STATISTICS_ENV_VARS}"
  [ -n "$env_vars" ] && argv+=(--env_vars "$env_vars")
  echo "[statistics] ${argv[*]:2}"
  (cd "$WORK" && "${argv[@]}" --target-path "$WORK/")
}

# Copy shared metadata into processed pool folders, as the CRoCS scripts do after each split.
share() {
  local source="$1" pool
  shift
  for pool in "$@"; do
    [ -d "$SCANNER/$pool" ] || continue
    local file
    for file in fee_rates.json false_cjtxs.json false_cjtxs.json.brief; do
      [ -f "$source/$file" ] && [ "$source" != "$SCANNER/$pool" ] && cp "$source/$file" "$SCANNER/$pool/"
    done
  done
  return 0
}

wasabi2() {
  parse ww2 process_dumplings
  # Per-pool fixes, run separately to lower memory requirements.
  parse ww2 "" "STREAMLINE_MIX_DATA=True;MIX_IDS=['wasabi2']"
  parse ww2 "" "FIX_WW2_FDNP=True;MIX_IDS=['wasabi2']"
  local pool
  for pool in wasabi2 wasabi2_others wasabi2_zksnacks; do
    cp "$DATA/wasabi2/fee_rates.json" "$DATA/wasabi2/false_cjtxs.json" "$DATA/wasabi2/txid_coord.json" "$SCANNER/$pool/"
  done
  parse ww2 detect_coordinators
  parse ww2 "" "ANALYZE_DETECT_COORDINATORS_ALG=True"
  parse ww2 detect_false_positives "MIX_IDS=['wasabi2']"
  parse ww2 "" "EXTRACT_TEMPORARY_FALSE_POSITIVES=True"
  parse ww2 split_coordinators
  share "$SCANNER/wasabi2" $(cd "$SCANNER" && find . -maxdepth 1 -type d -name 'wasabi2_*' ! -name wasabi2_others \
    ! -name wasabi2_zksnacks -printf '%f\n')
  parse ww2 "" "EXPORT_TX_FLAGS=True"
  parse ww2 "" "ANALYSIS_LIQUIDITY=True"
  parse ww2 plot_coinjoins "PLOT_REMIXES_AGGREGATE=True"
  parse ww2 plot_coinjoins "PLOT_REMIXES_SINGLE_INTERVAL=True"
}

wasabi1() {
  parse ww1 process_dumplings
  cp "$DATA/wasabi1/false_cjtxs.json" "$DATA/wasabi1/fee_rates.json" "$SCANNER/wasabi1/"
  parse ww1 detect_false_positives "MIX_IDS=['wasabi1']"
  parse ww1 split_coordinators
  share "$SCANNER/wasabi1" wasabi1_zksnacks wasabi1_mystery wasabi1_others
  parse ww1 "" "EXPORT_TX_FLAGS=True"
  parse ww1 "" "ANALYSIS_LIQUIDITY=True"
  parse ww1 plot_coinjoins "PLOT_REMIXES_AGGREGATE=True"
  parse ww1 plot_coinjoins "PLOT_REMIXES_SINGLE_INTERVAL=True"
}

joinmarket() {
  # Dumplings files Wasabi false positives as JoinMarket candidates; restoring them needs both
  # Wasabi analyses of this snapshot, which is why process_daily.sh runs JoinMarket last.
  if selected wasabi2 && selected wasabi1; then
    parse jm "" "RESTORE_FALSE_POSITIVES_FOR_OTHERS=True"
  else
    echo "[statistics] JoinMarket without Wasabi 1 and 2: Wasabi false positives are not restored"
  fi
  parse jm process_dumplings
  cp "$DATA/joinmarket/false_cjtxs.json" "$DATA/joinmarket/fee_rates.json" "$SCANNER/joinmarket_all/"
  parse jm detect_false_positives "MIX_IDS=['joinmarket_all']"
  parse jm "" "EXPORT_TX_FLAGS=True"
  parse jm "" "ANALYSIS_LIQUIDITY=True"
  parse jm plot_coinjoins "PLOT_REMIXES_AGGREGATE=True"
  parse jm plot_coinjoins "PLOT_REMIXES_SINGLE_INTERVAL=True;interval_start_date='2015-01-01 00:00:00.000'"
}

[ "$#" -gt 0 ] || { echo "Choose at least one protocol: wasabi2, wasabi1, joinmarket" >&2; exit 2; }
PROTOCOLS=("$@")
for protocol in "${PROTOCOLS[@]}"; do
  case "$protocol" in
    wasabi2 | wasabi1 | joinmarket) ;;
    *) echo "Unsupported protocol: $protocol" >&2; exit 2 ;;
  esac
done

# The processed coinjoin_tx_info.json files are large intermediates; only the site is published.
trap 'rm -rf "$WORK"' EXIT
rm -rf "$WORK" "$SITE"
mkdir -p "$WORK"
# Accept a dataset root with Scanner/ or a flat Dumplings snapshot.
if [ -d "$ROOT/input/Scanner" ]; then
  mv "$ROOT/input/Scanner" "$SCANNER"
else
  mv "$ROOT/input" "$SCANNER"
  mkdir "$ROOT/input"
fi
# coinjoin-analysis prefers <file>.json over a Dumplings file and never checks its age.
find "$SCANNER" -maxdepth 1 -name '*.txt.json' -delete
declare -A REQUIRED=([wasabi2]=Wasabi2CoinJoins.txt [wasabi1]=WasabiCoinJoins.txt [joinmarket]=OtherCoinJoins.txt)
for protocol in "${PROTOCOLS[@]}"; do
  [ -f "$SCANNER/${REQUIRED[$protocol]}" ] || {
    echo "The Dumplings snapshot has no ${REQUIRED[$protocol]} for $protocol" >&2
    exit 1
  }
done

# The order of process_daily.sh: JoinMarket needs the Wasabi results for its false positives.
for protocol in wasabi2 wasabi1 joinmarket; do
  if selected "$protocol"; then
    echo "[statistics] === $protocol ==="
    "$protocol"
  fi
done

python -m cj_process.file_check "$SCANNER/" || echo "[statistics] file_check reported problems" >&2
height="$(cat "$SCANNER/LastProcessedBlockHeight.txt" 2>/dev/null || true)"
python - "$SCANNER/summary.json" "$height" "${PROTOCOLS[@]}" <<'SUMMARY'
import datetime, json, sys

path, height, protocols = sys.argv[1], sys.argv[2].strip(), sys.argv[3:]
summary = {"date": datetime.date.today().strftime("%d-%m-%Y"), "lastProcessedBlockHeight": height}
summary["protocols"] = protocols
with open(path, "w", encoding="utf-8") as file:
    json.dump(summary, file)
SUMMARY

# The selection of backup_and_montage.sh, without the per-transaction data files.
(cd "$SCANNER" && find . -type f \( -name '*.json' -o -name '*.pdf' -o -name '*.png' -o -name '*.html' \
  -o -name coinjoin_results_check_summary.txt -o -name summary_processing_info.txt \
  -o -name summary_processing.success \) ! -name 'coinjoin_tx_info*.json' ! -name '*_events.json' \
  ! -name '*_false_filtered_cjtxs.json' -print0) | while IFS= read -r -d '' file; do
  mkdir -p "$SITE/$(dirname "$file")"
  cp "$SCANNER/$file" "$SITE/$file"
done
# Liquidity summaries of each pool, as embeddable HTML fragments; the site shows them as page texts.
mkdir -p "$SITE/texts"
find "$SITE" -maxdepth 1 -name '*.html' -exec mv -t "$SITE/texts/" {} +
echo "[statistics] site data: $(find "$SITE" -type f | wc -l) files"
