#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
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
exit 0
EOF
chmod +x "${FAKE_BIN}/docker"

export DOCKER_LOG

(
  cd "${PROJECT_DIR}"
  PATH="${FAKE_BIN}:${PATH}" \
  HOST_CLIENT_DIR="${PROJECT_DIR}/client" \
  COMPOSE_FILE="${PROJECT_DIR}/compose.yaml" \
  bash analysis.sh
)

if [[ ! -s "${DOCKER_LOG}" ]]; then
  echo "FAIL: docker was not called" >&2
  exit 1
fi

if ! grep -q -- "compose -f ${PROJECT_DIR}/compose.yaml -p blocksci-emulator --profile analysis rm -sf blocksci coinjoin_analysis " "${DOCKER_LOG}"; then
  echo "FAIL: expected analysis.sh to remove stale analysis containers before up" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

if ! grep -q -- "compose -f ${PROJECT_DIR}/compose.yaml -p blocksci-emulator --profile analysis up --build --force-recreate " "${DOCKER_LOG}"; then
  echo "FAIL: expected analysis.sh to force-recreate analysis containers" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

rm_line="$(grep -n -- " rm -sf blocksci coinjoin_analysis " "${DOCKER_LOG}" | head -n 1 | cut -d: -f1)"
up_line="$(grep -n -- " up --build --force-recreate " "${DOCKER_LOG}" | head -n 1 | cut -d: -f1)"
if [[ -z "${rm_line}" || -z "${up_line}" || "${rm_line}" -ge "${up_line}" ]]; then
  echo "FAIL: stale-container cleanup must happen before analysis up" >&2
  echo "Observed: $(cat "${DOCKER_LOG}")" >&2
  exit 1
fi

echo "PASS: analysis.sh removes stale analysis containers before recreating the analysis profile."
