#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

ISOLATED_PROJECT="${TMP_DIR}/repo/coinjoin-pipeline"
FAKE_BIN="${TMP_DIR}/bin"
RUN_LOG="${TMP_DIR}/run-all.log"
mkdir -p "${ISOLATED_PROJECT}/tests/pipeline" "${FAKE_BIN}" "${TMP_DIR}/repo"

cp "${PROJECT_DIR}/run-all.sh" "${ISOLATED_PROJECT}/run-all.sh"
cp "${PROJECT_DIR}/run-all-local.sh" "${ISOLATED_PROJECT}/run-all-local.sh"
chmod +x "${ISOLATED_PROJECT}/run-all.sh" "${ISOLATED_PROJECT}/run-all-local.sh"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "info" ]]; then
  exit 0
fi
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

cat >"${ISOLATED_PROJECT}/tests/test-command-builder-contract.sh" <<'EOF'
#!/usr/bin/env bash
exit 23
EOF
chmod +x "${ISOLATED_PROJECT}/tests/test-command-builder-contract.sh"

set +e
(
  cd "${ISOLATED_PROJECT}"
  EMULATION_LOGS_DIR="${ISOLATED_PROJECT}/emulation_logs" \
  PATH="${FAKE_BIN}:${PATH}" bash run-all-local.sh --skip-build --tests-only
) >"${RUN_LOG}" 2>&1
RUN_EXIT_CODE=$?
set -e

if [[ "${RUN_EXIT_CODE}" -ne 23 ]]; then
  echo "FAIL: expected failed child exit code 23, got ${RUN_EXIT_CODE}" >&2
  cat "${RUN_LOG}" >&2
  exit 1
fi

if ! grep -Fq "FAILED: test tests/test-command-builder-contract.sh (exit code 23). Logs: ${ISOLATED_PROJECT}/emulation_logs" "${RUN_LOG}"; then
  echo "FAIL: expected a named failure summary with the log directory" >&2
  cat "${RUN_LOG}" >&2
  exit 1
fi

echo "PASS: run-all-local.sh reports the failing workflow and log directory."
