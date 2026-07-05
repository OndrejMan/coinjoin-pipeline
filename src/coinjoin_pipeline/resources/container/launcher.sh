#!/usr/bin/env bash
# In-image launcher for the published BlockSci emulator pipeline.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"
WRAPPER_IMAGE="${WRAPPER_IMAGE:-ghcr.io/ondrejman/coinjoin-pipeline:latest}"
COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR:-${SCRIPT_DIR}/emulation_logs}"
NOTEBOOKS_DIR="${NOTEBOOKS_DIR:-${EMULATION_LOGS_DIR}/.notebooks}"
ORIGINAL_ARGS=("$@")

fail() { echo "ERROR: $*" >&2; exit 2; }

# Bash 3.2, still the default /bin/bash on macOS, treats "${empty_array[@]}"
# as an unbound variable under set -u. Expand optional arrays through the
# ${array[@]+...} guard at command boundaries where zero arguments are valid.

check_runtime() {
  [[ "${CONTAINER_RUNTIME}" == docker || "${CONTAINER_RUNTIME}" == podman ]] || fail "Unsupported container runtime '${CONTAINER_RUNTIME}' (expected docker or podman)"
  command -v "${CONTAINER_RUNTIME}" >/dev/null 2>&1 || fail "'${CONTAINER_RUNTIME}' command not found."
  "${CONTAINER_RUNTIME}" info >/dev/null 2>&1 || fail "${CONTAINER_RUNTIME} daemon/API is not reachable."
}

image_available() {
  local image="$1" registry_image="$1"
  "${CONTAINER_RUNTIME}" image inspect "${image}" >/dev/null 2>&1 && return 0
  [[ "${CONTAINER_RUNTIME}" == podman ]] && registry_image="docker://${image}"
  "${CONTAINER_RUNTIME}" manifest inspect "${registry_image}" >/dev/null 2>&1
}

require_image() {
  image_available "$1" || fail "required image is neither local nor accessible from its registry: $1"
}

resolve_podman_socket() {
  local socket_path
  for socket_path in "${CONTAINER_SOCKET:-}" "${PODMAN_SOCKET:-}" "${XDG_RUNTIME_DIR:-}/podman/podman.sock" "/run/user/$(id -u)/podman/podman.sock" "/run/podman/podman.sock"; do
    [[ -n "${socket_path}" && -S "${socket_path}" ]] && { printf '%s\n' "${socket_path}"; return 0; }
  done
  return 1
}

setup_socket() {
  INNER_CONTAINER_RUNTIME="${CONTAINER_RUNTIME}"
  CONTAINER_SOCKET_MOUNT="${CONTAINER_SOCKET:-/var/run/docker.sock}"
  CONTAINER_EXTRA_ARGS=()
  if [[ "${CONTAINER_RUNTIME}" == podman ]]; then
    CONTAINER_SOCKET_MOUNT="$(resolve_podman_socket)" || fail "Podman socket not found or not active. Set CONTAINER_SOCKET to the Podman API socket."
    INNER_CONTAINER_RUNTIME=docker
    CONTAINER_EXTRA_ARGS+=("-e" "DOCKER_HOST=unix:///var/run/docker.sock")
  fi
}

wrapper_pull_args() {
  if [[ "${CONTAINER_RUNTIME}" != docker ]]; then
    return 0
  fi
  local policy="${WRAPPER_PULL_POLICY:-}"
  if [[ -z "${policy}" ]]; then
    # Mirror wrapper.py container_run_pull_args(): published refs (with a
    # registry prefix) are pulled fresh, local-only tags are reused as-is.
    if [[ "${WRAPPER_IMAGE}" == */* ]]; then
      policy="always"
    else
      policy="missing"
    fi
  fi
  printf '%s\n' --pull "${policy}"
}

validate_wrapper_pull_policy() {
  [[ "${CONTAINER_RUNTIME}" == docker && -n "${WRAPPER_PULL_POLICY:-}" ]] || return 0
  case "${WRAPPER_PULL_POLICY}" in
    always|missing|never) ;;
    *) fail "Invalid WRAPPER_PULL_POLICY='${WRAPPER_PULL_POLICY}' (expected always, missing, or never)." ;;
  esac
}

run_research() {
  check_runtime
  require_image "${WRAPPER_IMAGE}"
  setup_socket
  validate_wrapper_pull_policy
  mkdir -p "${EMULATION_LOGS_DIR}"
  mkdir -p "${NOTEBOOKS_DIR}"
  local -a mounts=("-v" "${CONTAINER_SOCKET_MOUNT}:/var/run/docker.sock" "-v" "${EMULATION_LOGS_DIR}:${EMULATION_LOGS_DIR}:rw" "-v" "${SCRIPT_DIR}/scenarios:/app/scenarios:ro")
  local -a pull_args=()
  local pull_arg
  while IFS= read -r pull_arg; do
    pull_args+=("${pull_arg}")
  done < <(wrapper_pull_args)
  local index item value
  # External validation runs inside the wrapper, so make supplied inputs
  # visible there at their original absolute paths.
  for ((index=0; index<${#ORIGINAL_ARGS[@]}; index++)); do
    item="${ORIGINAL_ARGS[index]}"
    if [[ "${item}" == "--bitcoin-datadir" || "${item}" == "--baseline" || "${item}" == "--false-cjtxs" ]]; then
      value="${ORIGINAL_ARGS[index + 1]:-}"
      if [[ -n "${value}" && "${item}" == "--baseline" ]]; then
        value="$(dirname "${value}")"
      fi
      [[ -n "${value}" ]] && mounts+=("-v" "${value}:${value}:ro")
    fi
  done
  exec "${CONTAINER_RUNTIME}" run --rm \
    ${pull_args[@]+"${pull_args[@]}"} \
    -e "SCENARIOS_ROOT=/app/scenarios" -e "EMULATION_LOGS_DIR=${EMULATION_LOGS_DIR}" \
    ${CONTAINER_EXTRA_ARGS[@]+"${CONTAINER_EXTRA_ARGS[@]}"} "${mounts[@]}" \
    --entrypoint python3 "${WRAPPER_IMAGE}" -m client.research \
    --runs-root "${EMULATION_LOGS_DIR}" --runtime "${INNER_CONTAINER_RUNTIME}" "$@"
}

# Preserve the public researcher command surface, but execute it in the image.
if [[ "${1:-}" == container && ( "${2:-}" == docker || "${2:-}" == podman ) ]] && [[ "${3:-}" == runs || "${3:-}" == external || "${3:-}" == scenarios ]]; then
  CONTAINER_RUNTIME="$2"; shift 2
fi
if [[ "${1:-}" == runs || "${1:-}" == external || "${1:-}" == scenarios ]]; then
  run_research "$@"
fi

DRIVER=""; NAMESPACE=""; KUBECONFIG_PATH=""; PBS_BITCOIN_DATADIR_PATH="${PBS_BITCOIN_DATADIR:-}"; BLOCKSCI_SCRIPT_PATH=""; WRAPPER_ARGS=(); WRAPPER_EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    container) [[ $# -ge 2 ]] || fail "container requires docker or podman"; CONTAINER_RUNTIME="$2"; shift 2 ;;
    --driver) DRIVER="${2:-}"; shift 2 ;;
    --driver=*) DRIVER="${1#--driver=}"; shift ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --namespace=*) NAMESPACE="${1#--namespace=}"; shift ;;
    --kubeconfig) KUBECONFIG_PATH="${2:-}"; shift 2 ;;
    --kubeconfig=*) KUBECONFIG_PATH="${1#--kubeconfig=}"; shift ;;
    --pbs-bitcoin-datadir) PBS_BITCOIN_DATADIR_PATH="${2:-}"; shift 2 ;;
    --pbs-bitcoin-datadir=*) PBS_BITCOIN_DATADIR_PATH="${1#--pbs-bitcoin-datadir=}"; shift ;;
    --blocksci-script|--blocksciScript) BLOCKSCI_SCRIPT_PATH="${2:-}"; WRAPPER_ARGS+=("$1" "$2"); shift 2 ;;
    --blocksci-script=*|--blocksciScript=*) BLOCKSCI_SCRIPT_PATH="${1#*=}"; WRAPPER_ARGS+=("$1"); shift ;;
    *) WRAPPER_ARGS+=("$1"); shift ;;
  esac
done

SELECTED_ACTION=""; DOCTOR_ENGINE=""; DOCTOR_SCENARIO=""; HAS_HELP=false; HAS_DRY_RUN=false; HAS_YES=false; HAS_TEST_VALUES=false; HAS_ANALYSIS_PBS=false; HAS_BLOCKSCI_PBS=false; HAS_MAPPINGS_PBS=false; HAS_COPY_TO_HOST=false
for ((index=0; index<${#WRAPPER_ARGS[@]}; index++)); do
  item="${WRAPPER_ARGS[index]}"
  case "${item}" in
    full-run|recreate|clean|analyze|export|coinjoin-analysis|coinjoin|mappings|initialize) [[ -z "${SELECTED_ACTION}" ]] && SELECTED_ACTION="${item}" ;;
    --engine) DOCTOR_ENGINE="${WRAPPER_ARGS[index + 1]:-}" ;;
    --engine=*) DOCTOR_ENGINE="${item#--engine=}" ;;
    --scenario) DOCTOR_SCENARIO="${WRAPPER_ARGS[index + 1]:-}" ;;
    --scenario=*) DOCTOR_SCENARIO="${item#--scenario=}" ;;
    --help|-h) HAS_HELP=true ;;
    --dry-run) HAS_DRY_RUN=true ;;
    --yes) HAS_YES=true ;;
    --test-values) HAS_TEST_VALUES=true ;;
    --analysisPbs) HAS_ANALYSIS_PBS=true ;;
    --blocksciPbs) HAS_BLOCKSCI_PBS=true ;;
    --mappingsPbs) HAS_MAPPINGS_PBS=true ;;
    --copy-to-host) HAS_COPY_TO_HOST=true ;;
  esac
done
ACTION="${SELECTED_ACTION:-full-run}"
HAS_PBS_STAGE_DRY_RUN=false
if [[ "${HAS_DRY_RUN}" == true ]]; then
  if [[ "${ACTION}" == analyze && "${HAS_BLOCKSCI_PBS}" == true ]]; then HAS_PBS_STAGE_DRY_RUN=true; fi
  if [[ ( "${ACTION}" == coinjoin-analysis || "${ACTION}" == coinjoin ) && "${HAS_ANALYSIS_PBS}" == true ]]; then HAS_PBS_STAGE_DRY_RUN=true; fi
  if [[ "${ACTION}" == mappings && "${HAS_MAPPINGS_PBS}" == true ]]; then HAS_PBS_STAGE_DRY_RUN=true; fi
fi
if [[ "${HAS_HELP}" == false && ( "${ACTION}" == full-run || "${ACTION}" == recreate || "${ACTION}" == analyze || "${ACTION}" == export ) ]] && [[ "${DOCTOR_ENGINE}" != wasabi && "${DOCTOR_ENGINE}" != joinmarket ]]; then
  fail "${ACTION} requires --engine wasabi or --engine joinmarket."
fi

# PBS frontends already provide qsub and shared storage. This opt-in path keeps
# the documented runIt.sh interface while avoiding an unnecessary wrapper
# container that would hide the frontend's PBS client.
if [[ "${PBS_FRONTEND_DIRECT:-0}" == 1 && ( "${HAS_ANALYSIS_PBS}" == true || "${HAS_BLOCKSCI_PBS}" == true || "${HAS_MAPPINGS_PBS}" == true ) ]]; then
  DIRECT_WRAPPER_ARGS=("${WRAPPER_ARGS[@]}")
  [[ -z "${DRIVER}" ]] || DIRECT_WRAPPER_ARGS+=(--driver "${DRIVER}")
  [[ -z "${NAMESPACE}" ]] || DIRECT_WRAPPER_ARGS+=(--namespace "${NAMESPACE}")
  [[ -z "${KUBECONFIG_PATH}" ]] || DIRECT_WRAPPER_ARGS+=(--kubeconfig "${KUBECONFIG_PATH}")
  if [[ "${HAS_BLOCKSCI_PBS}" == true && -n "${PBS_BITCOIN_DATADIR_PATH}" ]]; then
    DIRECT_WRAPPER_ARGS+=(--pbs-bitcoin-datadir "${PBS_BITCOIN_DATADIR_PATH}")
  fi
  if [[ ( "${ACTION}" == full-run || "${ACTION}" == analyze || "${ACTION}" == export ) && "${HAS_TEST_VALUES}" == false ]]; then
    DIRECT_WRAPPER_ARGS+=(--test-values)
  fi
  DIRECT_WRAPPER_ROOT="${PBS_FRONTEND_WRAPPER_ROOT:-}"
  DIRECT_WRAPPER_SCRIPT="${DIRECT_WRAPPER_ROOT}/client/wrapper.py"
  DIRECT_WRAPPER_FROM_IMAGE=false
  if [[ ! -f "${DIRECT_WRAPPER_SCRIPT}" ]]; then
    DIRECT_WRAPPER_FROM_IMAGE=true
    check_runtime
    require_image "${WRAPPER_IMAGE}"
    DIRECT_WRAPPER_ROOT="${EMULATION_LOGS_DIR}/.pbs-wrapper-runtime"
    DIRECT_WRAPPER_SCRIPT="${DIRECT_WRAPPER_ROOT}/wrapper.py"
    if [[ ! -f "${DIRECT_WRAPPER_SCRIPT}" ]]; then
      DIRECT_WRAPPER_CONTAINER="pbs-wrapper-runtime-$$"
      mkdir -p "${DIRECT_WRAPPER_ROOT}"
      "${CONTAINER_RUNTIME}" create --name "${DIRECT_WRAPPER_CONTAINER}" "${WRAPPER_IMAGE}" >/dev/null
      if ! "${CONTAINER_RUNTIME}" cp "${DIRECT_WRAPPER_CONTAINER}:/app/." "${DIRECT_WRAPPER_ROOT}/"; then
        "${CONTAINER_RUNTIME}" rm -f "${DIRECT_WRAPPER_CONTAINER}" >/dev/null 2>&1 || true
        fail "could not extract PBS wrapper runtime from ${WRAPPER_IMAGE}"
      fi
      "${CONTAINER_RUNTIME}" rm -f "${DIRECT_WRAPPER_CONTAINER}" >/dev/null
    fi
  fi
  if [[ "${DIRECT_WRAPPER_FROM_IMAGE}" == true && -d "${SCRIPT_DIR}/tests/support/pbs-runtime" ]]; then
    cp -a "${SCRIPT_DIR}/tests/support/pbs-runtime/." "${DIRECT_WRAPPER_ROOT}/client/"
  fi
  export HOST_CLIENT_DIR="${DIRECT_WRAPPER_ROOT}/client"
  export SCENARIOS_DIR="${SCRIPT_DIR}/scenarios"
  export NOTEBOOKS_DIR
  export EMULATION_LOGS_DIR
  export EXPORTERS_DIR="${EXPORTERS_DIR:-${EMULATION_LOGS_DIR}/.wrapper-exporters}"
  mkdir -p "${EXPORTERS_DIR}"
  cp -a "${DIRECT_WRAPPER_ROOT}/exporters/." "${EXPORTERS_DIR}/"
  exec python3 "${DIRECT_WRAPPER_SCRIPT}" "${DIRECT_WRAPPER_ARGS[@]}"
fi
if [[ "${ACTION}" == analyze || "${ACTION}" == export || "${ACTION}" == coinjoin-analysis || "${ACTION}" == coinjoin || "${ACTION}" == mappings ]]; then
  HAS_SELECTED_RUN=false
  for item in ${WRAPPER_ARGS[@]+"${WRAPPER_ARGS[@]}"}; do
    [[ "${item}" == --all-runs || "${item}" == --run-dir || "${item}" == --run-dir=* ]] && HAS_SELECTED_RUN=true
  done
  [[ "${HAS_SELECTED_RUN}" == true ]] || fail "${ACTION} requires --run-dir <run-id>. Use './runIt.sh runs list' to select a run."
fi
[[ "${ACTION}" != clean || "${HAS_DRY_RUN}" == true || "${HAS_YES}" == true ]] || fail "clean is destructive; rerun with --yes or preview with --dry-run."

check_runtime
if [[ -n "${BLOCKSCI_SCRIPT_PATH}" ]]; then
  [[ -f "${BLOCKSCI_SCRIPT_PATH}" ]] || fail "BlockSci script not found: ${BLOCKSCI_SCRIPT_PATH}"
  BLOCKSCI_SCRIPT_PATH="$(cd "$(dirname "${BLOCKSCI_SCRIPT_PATH}")" && pwd)/$(basename "${BLOCKSCI_SCRIPT_PATH}")"
  for ((index=0; index<${#WRAPPER_ARGS[@]}; index++)); do
    case "${WRAPPER_ARGS[index]}" in
      --blocksci-script|--blocksciScript) WRAPPER_ARGS[index + 1]="${BLOCKSCI_SCRIPT_PATH}" ;;
      --blocksci-script=*|--blocksciScript=*) WRAPPER_ARGS[index]="${WRAPPER_ARGS[index]%%=*}=${BLOCKSCI_SCRIPT_PATH}" ;;
    esac
  done
fi
if [[ -n "${DOCTOR_SCENARIO}" && ! -f "${DOCTOR_SCENARIO}" && ! -f "${SCRIPT_DIR}/${DOCTOR_SCENARIO}" && ! -f "${SCRIPT_DIR}/scenarios/${DOCTOR_SCENARIO}" ]]; then fail "Scenario not found: ${DOCTOR_SCENARIO}"; fi
if [[ "${DRIVER}" == kubernetes ]]; then
  KUBE_CFG="${KUBECONFIG_PATH:-${HOME}/.kube/config}"
  [[ -f "${KUBE_CFG}" ]] || fail "kubeconfig not found at ${KUBE_CFG}"
  command -v kubectl >/dev/null 2>&1 && kubectl --kubeconfig "${KUBE_CFG}" get --raw=/version >/dev/null 2>&1 || fail "Kubernetes API is not reachable with ${KUBE_CFG}"
fi
DIRECT_KUBERNETES_BTC=false
if [[ "${DRIVER}" == kubernetes && ( "${ACTION}" == full-run || "${ACTION}" == recreate ) && "${HAS_COPY_TO_HOST}" == false ]]; then
  DIRECT_KUBERNETES_BTC=true
fi
if [[ "${HAS_BLOCKSCI_PBS}" == true && -n "${PBS_BITCOIN_DATADIR_PATH}" ]]; then
  if [[ "${DIRECT_KUBERNETES_BTC}" == true ]]; then
    mkdir -p "${PBS_BITCOIN_DATADIR_PATH}"
  else
    [[ -d "${PBS_BITCOIN_DATADIR_PATH}/regtest/blocks" ]] || fail "PBS Bitcoin datadir must contain regtest/blocks: ${PBS_BITCOIN_DATADIR_PATH}"
  fi
fi
require_image "${WRAPPER_IMAGE}"
echo "[doctor] OK: action=${ACTION} runtime=${CONTAINER_RUNTIME}${DOCTOR_ENGINE:+ engine=${DOCTOR_ENGINE}}"

if [[ "${HAS_DRY_RUN}" == true && "${HAS_PBS_STAGE_DRY_RUN}" == false ]]; then
  echo "[dry-run] No containers, Kubernetes resources, files, or reports will be created."
  echo "[dry-run] runtime: ${CONTAINER_RUNTIME}"
  echo "[dry-run] action: ${ACTION}"
  exit 0
fi

mkdir -p "${EMULATION_LOGS_DIR}"
EXPORTERS_DIR="${EXPORTERS_DIR:-${EMULATION_LOGS_DIR}/.wrapper-exporters}"
mkdir -p "${EXPORTERS_DIR}"
EMULATION_LOGS_DIR="$(cd "${EMULATION_LOGS_DIR}" && pwd)"; EXPORTERS_DIR="$(cd "${EXPORTERS_DIR}" && pwd)"
setup_socket
validate_wrapper_pull_policy
if [[ -n "${BLOCKSCI_SCRIPT_PATH}" ]]; then
  CONTAINER_EXTRA_ARGS+=("-v" "${BLOCKSCI_SCRIPT_PATH}:${BLOCKSCI_SCRIPT_PATH}:ro")
fi
if [[ -z "${COINJOIN_EMULATOR_DOCKER_PLATFORM:-}" && "${DOCTOR_ENGINE}" == joinmarket ]]; then
  case "$(uname -m)" in
    arm64|aarch64) COINJOIN_EMULATOR_DOCKER_PLATFORM=linux/amd64 ;;
  esac
fi
WRAPPER_PULL_ARGS=()
while IFS= read -r wrapper_pull_arg; do
  WRAPPER_PULL_ARGS+=("${wrapper_pull_arg}")
done < <(wrapper_pull_args)
if [[ "${ACTION}" == full-run || "${ACTION}" == analyze || "${ACTION}" == export ]] && [[ "${HAS_HELP}" == false && "${HAS_TEST_VALUES}" == false ]]; then WRAPPER_EXTRA_ARGS+=(--test-values); fi
if [[ "${DRIVER}" == kubernetes ]]; then
  KUBE_CFG="$(cd "$(dirname "${KUBE_CFG}")" && pwd)/$(basename "${KUBE_CFG}")"
  CONTAINER_EXTRA_ARGS+=("-v" "${KUBE_CFG}:${KUBE_CFG}:ro" "-v" "$(dirname "${KUBE_CFG}"):$(dirname "${KUBE_CFG}"):ro")
  WRAPPER_EXTRA_ARGS+=(--driver kubernetes --kubeconfig "${KUBE_CFG}")
  [[ -n "${NAMESPACE}" ]] && WRAPPER_EXTRA_ARGS+=(--namespace "${NAMESPACE}")
  [[ "${CONTAINER_RUNTIME}" == docker ]] && CONTAINER_EXTRA_ARGS+=(--add-host host.docker.internal:host-gateway)
  [[ -n "${KUBERNETES_CONTROL_IP:-}" ]] && CONTAINER_EXTRA_ARGS+=("-e" "KUBERNETES_CONTROL_IP=${KUBERNETES_CONTROL_IP}")
  if [[ "${DIRECT_KUBERNETES_BTC}" == true ]]; then
    CONTAINER_EXTRA_ARGS+=("-e" "KUBERNETES_STORAGE_UID=$(id -u)" "-e" "KUBERNETES_STORAGE_GID=$(id -g)")
  fi
fi
if [[ "${HAS_BLOCKSCI_PBS}" == true && -n "${PBS_BITCOIN_DATADIR_PATH}" ]]; then
  PBS_BITCOIN_DATADIR_PATH="$(cd "${PBS_BITCOIN_DATADIR_PATH}" && pwd)"
  PBS_BITCOIN_DATADIR_MODE=ro
  [[ "${DIRECT_KUBERNETES_BTC}" == false ]] || PBS_BITCOIN_DATADIR_MODE=rw
  CONTAINER_EXTRA_ARGS+=("-v" "${PBS_BITCOIN_DATADIR_PATH}:${PBS_BITCOIN_DATADIR_PATH}:${PBS_BITCOIN_DATADIR_MODE}")
  WRAPPER_EXTRA_ARGS+=(--pbs-bitcoin-datadir "${PBS_BITCOIN_DATADIR_PATH}")
fi

cleanup() {
  "${CONTAINER_RUNTIME}" stop "${WRAPPER_CONTAINER_NAME}" blocksci_analyzer coinjoin_analysis emulator_manager btc_data_wiper dind_image_prefetch isolated_docker_daemon >/dev/null 2>&1 || true
  if [[ "${CONTAINER_RUNTIME}" == podman ]]; then podman container cleanup --rm isolated_docker_daemon >/dev/null 2>&1 || true; podman rm -f -i emulator_manager btc_data_wiper dind_image_prefetch isolated_docker_daemon >/dev/null 2>&1 || true; podman network rm -f blocksci-emulator_default >/dev/null 2>&1 || true; fi
}
WRAPPER_CONTAINER_NAME="${WRAPPER_CONTAINER_NAME:-blocksci-runit-$$}"
WRAPPER_RUN_PID=""
handle_interrupt() {
  trap - INT TERM
  echo "Interrupted; stopping CoinJoin analysis containers..." >&2
  cleanup
  [[ -z "${WRAPPER_RUN_PID}" ]] || wait "${WRAPPER_RUN_PID}" >/dev/null 2>&1 || true
  exit 130
}
trap handle_interrupt INT TERM
POST_WRAPPER_SHELL="${POST_WRAPPER_SHELL:-0}"; BLOCKSCI_LAUNCH_JUPYTER="${BLOCKSCI_LAUNCH_JUPYTER:-${POST_WRAPPER_SHELL}}"
if [[ -z "${REPRODUCTION_COMMAND:-}" ]]; then
  REPRODUCTION_COMMAND="./runIt.sh"
  for item in ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"}; do
    printf -v item ' %q' "${item}"
    REPRODUCTION_COMMAND+="${item}"
  done
fi
set +e
"${CONTAINER_RUNTIME}" run --rm --name "${WRAPPER_CONTAINER_NAME}" \
  ${WRAPPER_PULL_ARGS[@]+"${WRAPPER_PULL_ARGS[@]}"} \
  -e "HOST_CLIENT_DIR=${SCRIPT_DIR}" -e "SCENARIOS_DIR=${SCRIPT_DIR}/scenarios" -e "NOTEBOOKS_DIR=${NOTEBOOKS_DIR}" \
  -e "EMULATION_LOGS_DIR=${EMULATION_LOGS_DIR}" -e "EXPORTERS_DIR=${EXPORTERS_DIR}" -e EXPORTERS_FROM_IMAGE=1 \
  -e "CONTAINER_RUNTIME=${INNER_CONTAINER_RUNTIME}" -e "WRAPPER_IMAGE=${WRAPPER_IMAGE}" \
  -e "BLOCKSCI_IMAGE=${BLOCKSCI_IMAGE:-}" -e "BLOCKSCI_PULL_POLICY=${BLOCKSCI_PULL_POLICY:-}" \
  -e "COINJOIN_ANALYSIS_IMAGE=${COINJOIN_ANALYSIS_IMAGE:-}" -e "COINJOIN_ANALYSIS_PULL_POLICY=${COINJOIN_ANALYSIS_PULL_POLICY:-}" \
  -e "COINJOIN_EMULATOR_IMAGE=${COINJOIN_EMULATOR_IMAGE:-}" -e "COINJOIN_EMULATOR_PULL_POLICY=${COINJOIN_EMULATOR_PULL_POLICY:-}" \
  -e "COINJOIN_EMULATOR_IMAGE_PREFIX=${COINJOIN_EMULATOR_IMAGE_PREFIX:-}" -e "COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD=${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-}" \
  -e "COINJOIN_EMULATOR_DOCKER_PLATFORM=${COINJOIN_EMULATOR_DOCKER_PLATFORM:-}" \
  -e WRAPPER_SCRIPT=wrapper.py -e "POST_WRAPPER_SHELL=${POST_WRAPPER_SHELL}" -e "BLOCKSCI_LAUNCH_JUPYTER=${BLOCKSCI_LAUNCH_JUPYTER}" -e "REPRODUCTION_COMMAND=${REPRODUCTION_COMMAND}" -e PYTHONDONTWRITEBYTECODE=1 \
  -v "${CONTAINER_SOCKET_MOUNT}:/var/run/docker.sock" -v "${SCRIPT_DIR}/scenarios:/app/scenarios:ro" -v "${NOTEBOOKS_DIR}:/app/notebooks:rw" \
  -v "${EMULATION_LOGS_DIR}:${EMULATION_LOGS_DIR}:rw" -v "${EXPORTERS_DIR}:${EXPORTERS_DIR}:rw" ${CONTAINER_EXTRA_ARGS[@]+"${CONTAINER_EXTRA_ARGS[@]}"} \
  --entrypoint /bin/bash "${WRAPPER_IMAGE}" -c 'set -euo pipefail; mkdir -p "${EXPORTERS_DIR}"; cp -a /app/exporters/. "${EXPORTERS_DIR}/"; python3 "${WRAPPER_SCRIPT}" "$@"; [[ "${POST_WRAPPER_SHELL:-0}" != 1 ]] || exec /bin/bash' _ ${WRAPPER_ARGS[@]+"${WRAPPER_ARGS[@]}"} ${WRAPPER_EXTRA_ARGS[@]+"${WRAPPER_EXTRA_ARGS[@]}"} 0<&0 &
WRAPPER_RUN_PID=$!
wait "${WRAPPER_RUN_PID}"
exit_code=$?
WRAPPER_RUN_PID=""
set -e
trap - INT TERM
[[ "${CONTAINER_RUNTIME}" != podman ]] || cleanup
[[ "${exit_code}" == 0 || "${exit_code}" == 2 || "${exit_code}" == 3 || "${exit_code}" == 4 || "${exit_code}" == 130 ]] && exit "${exit_code}"
exit 5
