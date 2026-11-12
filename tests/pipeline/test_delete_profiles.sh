#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_DIR="${REPO_ROOT}/pipeline"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

FAKE_BIN="${TMP_DIR}/bin"
DOCKER_LOG="${TMP_DIR}/docker.args"
mkdir -p "${FAKE_BIN}"

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG

PATH="${FAKE_BIN}:${PATH}" \
  COMPOSE_FILE="${PROJECT_DIR}/compose.yaml" \
  bash "${PROJECT_DIR}/delete.sh"

if ! grep -q -- "compose -f ${PROJECT_DIR}/compose.yaml -p blocksci-emulator --profile emulate --profile analysis down --remove-orphans " "${DOCKER_LOG}"; then
  echo "FAIL: delete.sh did not activate both profiles while removing the Compose stack" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if grep -q -- "volume rm .*dind_cache" "${DOCKER_LOG}"; then
  echo "FAIL: delete.sh removed the persistent DinD image cache" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

# --- abandoned projects ---------------------------------------------------------
# The project name carries the launching pid, so the teardown above only ever
# addresses the project this run is about to create. Earlier runs' projects are
# reclaimed here or never — each holds a Docker subnet, and once the address
# pools ran dry a suite died at k3d cluster creation.
REAP_LOG="${TMP_DIR}/reap.args"

# A process that is certainly alive for the duration of the assertion, and one
# pid that is certainly not.
sleep 120 &
LIVE_PID=$!
trap 'kill "${LIVE_PID}" 2>/dev/null; cleanup' EXIT

cat >"${FAKE_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >>"${DOCKER_LOG:?}"
printf '\n' >>"${DOCKER_LOG:?}"
if [[ "$1" == "network" && "$2" == "ls" ]]; then
  printf 'bridge\ncjp-overactive-local-999001_default\ncjp-overactive-local-%s_default\n' "${LIVE_PID:?}"
elif [[ "$1" == "ps" ]]; then
  printf 'cjp-overactive-local-999001\n\n'
elif [[ "$1" == "volume" && "$2" == "ls" ]]; then
  # A project whose network and containers are already gone: volumes only.
  printf 'cjp-overactive-local-999003\n\n'
fi
EOF
chmod +x "${FAKE_BIN}/docker"

DOCKER_LOG="${REAP_LOG}"
export DOCKER_LOG LIVE_PID

PATH="${FAKE_BIN}:${PATH}" \
  COMPOSE_FILE="${PROJECT_DIR}/compose.yaml" \
  COINJOIN_COMPOSE_PROJECT="cjp-current-4242" \
  bash "${PROJECT_DIR}/delete.sh" >/dev/null

if ! grep -q -- "-p cjp-overactive-local-999001 " "${REAP_LOG}"; then
  echo "FAIL: delete.sh left an abandoned Compose project behind (its subnet leaks)" >&2
  echo "Observed: $(cat "${REAP_LOG}")" >&2
  exit 1
fi

# An abandoned pid-scoped project never recurs, so its dind_cache is dead
# weight; the current project's own teardown must still keep it (asserted above).
if ! grep -- "-p cjp-overactive-local-999001 " "${REAP_LOG}" | grep -q -- "--volumes"; then
  echo "FAIL: delete.sh kept the volumes of an abandoned project (its image cache leaks disk)" >&2
  echo "Observed: $(cat "${REAP_LOG}")" >&2
  exit 1
fi

if ! grep -- "-p cjp-overactive-local-999003 " "${REAP_LOG}" | grep -q -- "--volumes"; then
  echo "FAIL: delete.sh missed a project that survives only as volumes" >&2
  echo "Observed: $(cat "${REAP_LOG}")" >&2
  exit 1
fi

if grep -q -- "-p cjp-overactive-local-${LIVE_PID} " "${REAP_LOG}"; then
  echo "FAIL: delete.sh tore down a project whose process is still running" >&2
  echo "Observed: $(cat "${REAP_LOG}")" >&2
  exit 1
fi

echo "PASS: delete.sh removes profiled containers while preserving the DinD image cache."
echo "PASS: delete.sh reaps abandoned Compose projects and spares live ones."
