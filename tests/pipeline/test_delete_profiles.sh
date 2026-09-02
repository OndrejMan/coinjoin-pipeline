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

echo "PASS: delete.sh removes profiled containers while preserving the DinD image cache."
