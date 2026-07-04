#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

mkdir -p "${TMP_DIR}/bin" "${TMP_DIR}/logs"
touch "${TMP_DIR}/kubeconfig"

cat >"${TMP_DIR}/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >>"${FAKE_DOCKER_ARGS:?}"
EOF
chmod 0755 "${TMP_DIR}/bin/docker"

FAKE_DOCKER_ARGS="${TMP_DIR}/docker.args" \
PATH="${TMP_DIR}/bin:${PATH}" \
"${PROJECT_DIR}/run-pipeline-image.sh" --build --setup "${TMP_DIR}/kubeconfig" --logs-dir "${TMP_DIR}/logs" \
  recreate --engine wasabi --namespace thesis-test

rg -Fx -- build "${TMP_DIR}/docker.args"
rg -Fx -- "${TMP_DIR}/kubeconfig:/root/.kube/config:ro" "${TMP_DIR}/docker.args"
rg -Fx -- "${TMP_DIR}/logs:/runs:rw" "${TMP_DIR}/docker.args"
rg -Fx -- "/var/run/docker.sock:/var/run/docker.sock" "${TMP_DIR}/docker.args"
rg -Fx -- --driver "${TMP_DIR}/docker.args"
rg -Fx -- kubernetes "${TMP_DIR}/docker.args"
rg -Fx -- --kubeconfig "${TMP_DIR}/docker.args"
rg -Fx -- /root/.kube/config "${TMP_DIR}/docker.args"
if PATH="${TMP_DIR}/bin:${PATH}" "${PROJECT_DIR}/run-pipeline-image.sh" full-run --engine wasabi >/dev/null 2>&1; then
  echo "FAIL: --setup must be required" >&2
  exit 1
fi

echo "PASS: pipeline image runner uses the selected host runtime socket without sibling repositories."
