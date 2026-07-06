#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULT_ROOT="${TEST_RESULT_DIR:-$(mktemp -d)}"
REMOVE_RESULT_ROOT=0
if [[ -z "${TEST_RESULT_DIR:-}" ]]; then
  REMOVE_RESULT_ROOT=1
fi

cleanup() {
  if (( REMOVE_RESULT_ROOT == 1 )); then
    rm -rf "${RESULT_ROOT}"
  fi
}
trap cleanup EXIT

INJECT_KUBERNETES_CLIENT_FAILURE=1 \
TEST_RESULT_DIR="${RESULT_ROOT}" \
  "${SCRIPT_DIR}/test-kubernetes-pbs-analysis.sh" wasabi

DIAGNOSTICS="${RESULT_ROOT}/wasabi/kubernetes-diagnostics/diagnostics.txt"
[[ -s "${DIAGNOSTICS}" ]] || {
  echo "FAIL: expected diagnostics artifact was not created: ${DIAGNOSTICS}" >&2
  exit 1
}
grep -ERq 'wasabi-client-002|NotFound|pod was deleted' \
  "${RESULT_ROOT}/wasabi/kubernetes-diagnostics" || {
  echo "FAIL: diagnostics do not identify the deleted client pod" >&2
  exit 1
}

echo "PASS: missing Kubernetes client produced a failed inner pipeline and diagnostics artifact"
