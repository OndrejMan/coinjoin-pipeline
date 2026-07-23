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
CHILD_STARTED="${TMP_DIR}/child.started"
CHILD_INTERRUPTED="${TMP_DIR}/child.interrupted"
mkdir -p "${ISOLATED_PROJECT}/tests/pipeline" "${FAKE_BIN}" "${TMP_DIR}/repo"

cp "${PROJECT_DIR}/run-all-local.sh" "${ISOLATED_PROJECT}/run-all-local.sh"
cp "${PROJECT_DIR}/run-all.sh" "${ISOLATED_PROJECT}/run-all.sh"
chmod +x "${ISOLATED_PROJECT}/run-all-local.sh" "${ISOLATED_PROJECT}/run-all.sh"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "info" ]]; then
  exit 0
fi
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

cat >"${ISOLATED_PROJECT}/tests/test-runIt-overactive-local.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
trap 'touch "${CHILD_INTERRUPTED:?}"; exit 130' INT TERM
touch "${CHILD_STARTED:?}"
while true; do
  sleep 1
done
EOF
chmod +x "${ISOLATED_PROJECT}/tests/test-runIt-overactive-local.sh"

for test_script in \
  test-command-builder-contract.sh \
  pipeline/test_emulate_exit_status.sh \
  pipeline/test_emulate_interrupt_cleanup.sh \
  test-podman-no-host-docker.sh \
  test-runIt-overactive-local-docker.sh \
  test-runIt-joinmarket-local-docker.sh \
  test-runIt-parallel-local-docker.sh \
  test-kubernetes-k3d.sh \
  test-kubernetes-pbs-analysis.sh \
  test-parallel-pbs-analysis.sh \
  test-kubernetes-s3-minio.sh
do
  cat >"${ISOLATED_PROJECT}/tests/${test_script}" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "${ISOLATED_PROJECT}/tests/${test_script}"
done

(
  cd "${ISOLATED_PROJECT}"
  PATH="${FAKE_BIN}:${PATH}" \
  CHILD_STARTED="${CHILD_STARTED}" \
  CHILD_INTERRUPTED="${CHILD_INTERRUPTED}" \
  bash run-all-local.sh --skip-build --tests-only
) >"${RUN_LOG}" 2>&1 &
RUN_PID=$!

for _ in {1..50}; do
  [[ -e "${CHILD_STARTED}" ]] && break
  sleep 0.1
done

if [[ ! -e "${CHILD_STARTED}" ]]; then
  echo "FAIL: run-all-local.sh did not start the first child test" >&2
  cat "${RUN_LOG}" >&2 || true
  kill "${RUN_PID}" >/dev/null 2>&1 || true
  wait "${RUN_PID}" >/dev/null 2>&1 || true
  exit 1
fi

kill -TERM "${RUN_PID}"
set +e
wait "${RUN_PID}"
RUN_EXIT_CODE=$?
set -e

if [[ "${RUN_EXIT_CODE}" -ne 130 ]]; then
  echo "FAIL: expected run-all-local.sh to exit 130 after TERM, got ${RUN_EXIT_CODE}" >&2
  cat "${RUN_LOG}" >&2 || true
  exit 1
fi

if [[ ! -e "${CHILD_INTERRUPTED}" ]]; then
  echo "FAIL: expected run-all-local.sh to forward TERM to the active child" >&2
  cat "${RUN_LOG}" >&2 || true
  exit 1
fi

if ! grep -q -- "Interrupted; stopping current local workflow" "${RUN_LOG}"; then
  echo "FAIL: expected interrupt cleanup message" >&2
  cat "${RUN_LOG}" >&2 || true
  exit 1
fi

echo "PASS: run-all-local.sh forwards interrupts to the active child workflow."
