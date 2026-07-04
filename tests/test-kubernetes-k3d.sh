#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP_DIR="$(mktemp -d)"

CLUSTER_NAME="${CLUSTER_NAME:-coinjoin-k3d-$$}"
NAMESPACE="${NAMESPACE:-coinjoin-itest-$$}"
SERVERS="${SERVERS:-1}"
AGENTS="${AGENTS:-2}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180s}"
SCENARIO="${SCENARIO:-overactive-local.json}"
ACTION="${ACTION:-recreate}"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/blocksciemulatoranalysis:latest}"
EMULATOR_IMAGE="${EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
# The wrapper reads COINJOIN_EMULATOR_IMAGE. Keep it aligned with the image
# selected for this test so local-image validation does not fall back to GHCR.
COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-${EMULATOR_IMAGE}}"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/ondrejman/}"
KEEP_CLUSTER="${KEEP_CLUSTER:-0}"
PRE_CLEANUP="${PRE_CLEANUP:-1}"
PRE_CLEANUP_PREFIX="${PRE_CLEANUP_PREFIX-coinjoin-k3d-}"
PRE_CLEANUP_CONTAINERS="${PRE_CLEANUP_CONTAINERS:-1}"
K3D_NODEPORT_RANGE="${K3D_NODEPORT_RANGE:-30000-30029}"

if [[ -z "${CONTAINER_KUBE_HOST:-}" ]]; then
  if [[ "${CONTAINER_RUNTIME}" == "docker" ]]; then
    CONTAINER_KUBE_HOST="$("${CONTAINER_RUNTIME}" network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
    CONTAINER_KUBE_HOST="${CONTAINER_KUBE_HOST:-host.docker.internal}"
  else
    CONTAINER_KUBE_HOST="host.containers.internal"
  fi
fi
export WRAPPER_IMAGE
export COINJOIN_EMULATOR_IMAGE
export KUBERNETES_CONTROL_IP="${CONTAINER_KUBE_HOST}"

HOST_KUBECONFIG="${TMP_DIR}/kubeconfig-host.yaml"
CONTAINER_KUBECONFIG="${TMP_DIR}/kubeconfig-container.yaml"

cleanup() {
  if [[ "${KEEP_CLUSTER}" != "1" ]]; then
    k3d cluster delete "${CLUSTER_NAME}" >/dev/null 2>&1 || true
  else
    echo "KEEP_CLUSTER=1; leaving k3d cluster '${CLUSTER_NAME}' running."
    echo "Host kubeconfig: ${HOST_KUBECONFIG}"
  fi
  if [[ "${KEEP_CLUSTER}" != "1" ]]; then
    rm -rf "${TMP_DIR}"
  fi
}
trap cleanup EXIT

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "FAIL: required command not found: $1" >&2
    exit 2
  fi
}

pre_cleanup_clusters() {
  local cluster_names=()
  local cluster_name
  local cluster_json
  local cluster_names_file

  if [[ "${PRE_CLEANUP}" != "1" ]]; then
    return 0
  fi
  if [[ -z "${PRE_CLEANUP_PREFIX}" ]]; then
    echo "FAIL: PRE_CLEANUP_PREFIX must not be empty when PRE_CLEANUP=1." >&2
    exit 2
  fi

  echo "Cleaning up existing k3d test clusters matching '${PRE_CLEANUP_PREFIX}*'..."
  cluster_json="$(k3d cluster list -o json)"
  cluster_names_file="${TMP_DIR}/pre-cleanup-clusters.txt"
  python3 -c '
import json
import sys

target_name, cleanup_prefix = sys.argv[1], sys.argv[2]
clusters = json.loads(sys.argv[3])
if isinstance(clusters, dict):
    clusters = clusters.get("clusters", [])
for cluster in clusters:
    name = cluster.get("name") or cluster.get("Name") or ""
    if name == target_name or name.startswith(cleanup_prefix):
        print(name)
' "${CLUSTER_NAME}" "${PRE_CLEANUP_PREFIX}" "${cluster_json}" >"${cluster_names_file}"
  mapfile -t cluster_names <"${cluster_names_file}"

  for cluster_name in "${cluster_names[@]}"; do
    echo "Deleting existing k3d cluster '${cluster_name}'..."
    k3d cluster delete "${cluster_name}" >/dev/null
  done
}

pre_cleanup_running_containers() {
  local container_ids=()
  local container_id

  if [[ "${PRE_CLEANUP}" != "1" || "${PRE_CLEANUP_CONTAINERS}" != "1" ]]; then
    return 0
  fi

  mapfile -t container_ids < <("${CONTAINER_RUNTIME}" ps -q)
  if [[ "${#container_ids[@]}" -eq 0 ]]; then
    echo "No running ${CONTAINER_RUNTIME} containers to kill before test."
    return 0
  fi

  echo "Killing ${#container_ids[@]} running ${CONTAINER_RUNTIME} container(s) before test..."
  for container_id in "${container_ids[@]}"; do
    "${CONTAINER_RUNTIME}" kill "${container_id}" >/dev/null 2>&1 || true
  done
}

podman_socket() {
  local socket_path

  for socket_path in \
    "${CONTAINER_SOCKET:-}" \
    "${PODMAN_SOCKET:-}" \
    "${XDG_RUNTIME_DIR:-}/podman/podman.sock" \
    "/run/user/$(id -u)/podman/podman.sock" \
    "/run/podman/podman.sock"
  do
    if [[ -n "${socket_path}" && -S "${socket_path}" ]]; then
      printf '%s\n' "${socket_path}"
      return 0
    fi
  done

  return 1
}

rewrite_kubeconfig_for_container() {
  local source_kubeconfig="$1"
  local target_kubeconfig="$2"
  local api_server
  local api_port
  local cluster_name

  cp "${source_kubeconfig}" "${target_kubeconfig}"

  api_server="$(kubectl --kubeconfig "${source_kubeconfig}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
  api_port="${api_server##*:}"
  api_port="${api_port%%/*}"
  cluster_name="$(kubectl --kubeconfig "${target_kubeconfig}" config view --minify -o jsonpath='{.contexts[0].context.cluster}')"

  if [[ -z "${api_port}" || "${api_port}" == "${api_server}" ]]; then
    echo "FAIL: could not extract API server port from kubeconfig server: ${api_server}" >&2
    exit 1
  fi

  kubectl --kubeconfig "${target_kubeconfig}" \
    config set-cluster "${cluster_name}" \
    --server="https://${CONTAINER_KUBE_HOST}:${api_port}" \
    --insecure-skip-tls-verify=true >/dev/null
  kubectl --kubeconfig "${target_kubeconfig}" \
    config unset "clusters.${cluster_name}.certificate-authority-data" >/dev/null 2>&1 || true
  kubectl --kubeconfig "${target_kubeconfig}" \
    config unset "clusters.${cluster_name}.certificate-authority" >/dev/null 2>&1 || true

  echo "Container kubeconfig API server: https://${CONTAINER_KUBE_HOST}:${api_port}"
}

require_command k3d
require_command kubectl
require_command "${CONTAINER_RUNTIME}"
require_command python3

pre_cleanup_running_containers
pre_cleanup_clusters

pull_image() {
  local image="$1"
  if [[ "${image}" != */* ]]; then
    echo "Using local image: ${image}"
    return 0
  fi

  echo "Pulling latest artifact image: ${image}"
  "${CONTAINER_RUNTIME}" pull "${image}"
}

if [[ ! -f "${PROJECT_DIR}/scenarios/${SCENARIO}" && ! -f "${SCENARIO}" ]]; then
  echo "FAIL: scenario not found: ${SCENARIO}" >&2
  echo "Pass SCENARIO=<file> or keep it under ${PROJECT_DIR}/scenarios." >&2
  exit 2
fi
if [[ -f "${PROJECT_DIR}/scenarios/${SCENARIO}" ]]; then
  SCENARIO_PATH="${PROJECT_DIR}/scenarios/${SCENARIO}"
else
  SCENARIO_PATH="${SCENARIO}"
fi

if [[ "${CONTAINER_RUNTIME}" == "podman" ]]; then
  if ! CONTAINER_SOCKET="$(podman_socket)"; then
    echo "FAIL: Podman socket not found." >&2
    echo "Start it with: systemctl --user enable --now podman.socket" >&2
    echo "Or pass CONTAINER_SOCKET=/path/to/podman.sock." >&2
    exit 2
  fi
  export CONTAINER_SOCKET
  echo "Using Podman socket: ${CONTAINER_SOCKET}"
fi

mapfile -t ARTIFACT_IMAGES < <(
  python3 - "${SCENARIO_PATH}" "${IMAGE_PREFIX}" <<'PY'
import json
import sys

scenario_path, image_prefix = sys.argv[1], sys.argv[2]
with open(scenario_path, "r", encoding="utf-8") as scenario_file:
    scenario = json.load(scenario_file)

versions = {scenario.get("default_version") or "2.6.0"}
if scenario.get("distributor_version"):
    versions.add(scenario["distributor_version"])
for wallet in scenario.get("wallets", []):
    if wallet.get("version"):
        versions.add(wallet["version"])

print(f"{image_prefix}btc-node")
for version in sorted(versions):
    print(f"{image_prefix}wasabi-client:{version}")

if any(version >= "2.6.0" for version in versions):
    print(f"{image_prefix}wasabi-backend:2.6.0")
    print(f"{image_prefix}wasabi-coordinator:2.6.0")
else:
    print(f"{image_prefix}wasabi-backend:2.0.4")
PY
)

pull_image "${WRAPPER_IMAGE}"
pull_image "${EMULATOR_IMAGE}"
for image in "${ARTIFACT_IMAGES[@]}"; do
  pull_image "${image}"
done

echo "Creating k3d cluster '${CLUSTER_NAME}' with ${SERVERS} server(s) and ${AGENTS} worker agent(s)..."
k3d cluster create "${CLUSTER_NAME}" \
  --servers "${SERVERS}" \
  --agents "${AGENTS}" \
  --wait \
  --timeout "${WAIT_TIMEOUT}" \
  -p "${K3D_NODEPORT_RANGE}:${K3D_NODEPORT_RANGE}@server:0" \
  --k3s-arg "--kube-apiserver-arg=service-node-port-range=${K3D_NODEPORT_RANGE}@server:*"

k3d kubeconfig get "${CLUSTER_NAME}" >"${HOST_KUBECONFIG}"

echo "Waiting for all Kubernetes nodes to be Ready..."
kubectl --kubeconfig "${HOST_KUBECONFIG}" \
  wait node --all --for=condition=Ready --timeout="${WAIT_TIMEOUT}"

node_count="$(kubectl --kubeconfig "${HOST_KUBECONFIG}" get nodes --no-headers | wc -l | tr -d ' ')"
expected_nodes=$((SERVERS + AGENTS))
if [[ "${node_count}" -lt "${expected_nodes}" ]]; then
  echo "FAIL: expected at least ${expected_nodes} nodes, found ${node_count}" >&2
  kubectl --kubeconfig "${HOST_KUBECONFIG}" get nodes -o wide >&2
  exit 1
fi

echo "Cluster nodes:"
kubectl --kubeconfig "${HOST_KUBECONFIG}" get nodes -o wide

rewrite_kubeconfig_for_container "${HOST_KUBECONFIG}" "${CONTAINER_KUBECONFIG}"

echo "Running Kubernetes emulation test action '${ACTION}' in namespace '${NAMESPACE}'..."
(
  cd "${PROJECT_DIR}"
  ./runIt.sh container "${CONTAINER_RUNTIME}" "${ACTION}" \
    --engine wasabi \
    --scenario "${SCENARIO}" \
    --driver kubernetes \
    --namespace "${NAMESPACE}" \
    --kubeconfig "${CONTAINER_KUBECONFIG}" \
    --image-prefix "${IMAGE_PREFIX}" \
    --copy-to-host \
    "$@"
)

echo "PASS: Kubernetes emulation path completed on k3d cluster '${CLUSTER_NAME}' with ${AGENTS} worker agent(s)."
