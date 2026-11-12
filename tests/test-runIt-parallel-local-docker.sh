#!/usr/bin/env bash
# Happy-path e2e: full-run --parallel on the local Docker runtime.
# Both the coinjoin-analysis and BlockSci analyzers must be scheduled
# concurrently under the wrapper's "Parallel analysis" pipeline stage,
# and the unified report export must still produce the expected artifacts.
# Arguments and environment as in test-runIt-overactive-local-docker.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

E2E_PARALLEL=1 exec bash "${SCRIPT_DIR}/test-runIt-overactive-local-docker.sh" "$@"
