#!/usr/bin/env bash
# Happy-path e2e: full-run --parallel with both analyzers scheduled on PBS.
# Kubernetes runs the emulation; --analysis-pbs --blocksci-pbs --parallel must
# submit the coinjoin-analysis and BlockSci PBS jobs concurrently under the
# wrapper's "Parallel analysis" pipeline stage, join them, and then run the
# unified report export PBS stage. Arguments and environment as in
# test-kubernetes-pbs-analysis.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

E2E_PARALLEL=1 exec bash "${SCRIPT_DIR}/test-kubernetes-pbs-analysis.sh" "$@"
