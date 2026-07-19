#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs}"
IMAGE="${PBS_IMAGE:-bitcoin-analysis/pbs-apptainer:23.06.06-1.5.1}"
HOSTNAME="${PBS_HOSTNAME:-pbs}"
WORKDIR_HOST="${PBS_WORKDIR_HOST:-$PWD}"
WORKDIR_CONTAINER="${PBS_WORKDIR_CONTAINER:-${WORKDIR_HOST}}"
IMAGE_CONTEXT="${PBS_IMAGE_CONTEXT:-${SCRIPT_DIR}}"

docker() {
  if /usr/bin/docker info >/dev/null 2>&1; then
    /usr/bin/docker "$@"
  else
    sudo /usr/bin/docker "$@"
  fi
}

build() {
  docker build --pull \
    --build-arg OPENPBS_VERSION="${OPENPBS_VERSION:-23.06.06}" \
    --build-arg APPTAINER_VERSION="${APPTAINER_VERSION:-1.5.1}" \
    -t "${IMAGE}" "${IMAGE_CONTEXT}"
}

require_storage_paths() {
  for path in "${WORKDIR_HOST}" "${WORKDIR_CONTAINER}"; do
    if [[ "${path}" != /storage/* ]]; then
      echo "Local PBS parity mode requires paths below /storage: ${path}" >&2
      exit 2
    fi
  done
  if [[ ! -d /storage ]]; then
    echo "Local PBS parity mode requires a host /storage mount." >&2
    exit 2
  fi
  if [[ ! -w "${WORKDIR_HOST}" ]]; then
    echo "PBS work directory must be writable: ${WORKDIR_HOST}" >&2
    exit 2
  fi
}

configure_server() {
  for i in {1..60}; do
    if docker exec -u root "${CONTAINER_NAME}" bash -lc '
      set -e
      qmgr -c "create resource scratch_local type=size,flag=h" 2>/dev/null || true
      qmgr -c "create node pbs" 2>/dev/null || true
      qmgr -c "set node pbs queue=workq" 2>/dev/null || true
      qmgr -c "set node pbs resources_available.ncpus=8"
      qmgr -c "set node pbs resources_available.mem=16gb"
      qmgr -c "set node pbs resources_available.scratch_local=20gb"
      qmgr -c "create queue workq queue_type=execution" 2>/dev/null || true
      qmgr -c "set queue workq enabled=true"
      qmgr -c "set queue workq started=true"
      qmgr -c "set server default_queue=workq"
      qmgr -c "set server job_history_enable=true"
      qmgr -c "create hook local_scratch" 2>/dev/null || true
      qmgr -c "set hook local_scratch enabled=true,event=\"execjob_launch,execjob_end\""
      qmgr -c "import hook local_scratch application/x-python default /opt/local-pbs/local_scratch_hook.py"
    ' >/dev/null 2>&1; then
      return
    fi
    if [[ "${i}" == 60 ]]; then
      echo "PBS configuration did not succeed in time." >&2
      docker logs "${CONTAINER_NAME}" >&2 || true
      exit 1
    fi
    sleep 1
  done
}

smoke_test_apptainer() {
  local smoke_dir="${WORKDIR_HOST}/.pbs-apptainer-smoke"
  mkdir -p "${smoke_dir}"
  printf 'bind-ok\n' >"${smoke_dir}/probe.txt"
  chmod -R a+rX "${smoke_dir}"

  docker exec -u pbsuser "${CONTAINER_NAME}" singularity --version
  docker exec -u pbsuser "${CONTAINER_NAME}" bash -lc \
    'export SINGULARITY_CACHEDIR=/scratch/smoke-cache SINGULARITY_TMPDIR=/scratch/smoke-tmp; mkdir -p "$SINGULARITY_CACHEDIR" "$SINGULARITY_TMPDIR"; singularity exec docker://alpine:3.20 true'
  docker exec -u pbsuser "${CONTAINER_NAME}" singularity exec \
    --bind "${smoke_dir}:/parity-smoke:ro" docker://alpine:3.20 \
    grep -qx bind-ok /parity-smoke/probe.txt
  rm -rf "${smoke_dir}"
}

start() {
  require_storage_paths
  if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "PBS/Apptainer image is missing; building ${IMAGE}..."
    build
  fi

  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker run -dit \
    --privileged \
    --name "${CONTAINER_NAME}" \
    --hostname "${HOSTNAME}" \
    -e PBS_HOSTNAME="${HOSTNAME}" \
    -e PBS_START_MOM=1 \
    -v /storage:/storage \
    -w "${WORKDIR_CONTAINER}" \
    "${IMAGE}" >/dev/null

  echo "Waiting for PBS server..."
  for i in {1..60}; do
    if docker exec "${CONTAINER_NAME}" bash -lc 'qstat -B >/dev/null 2>&1'; then
      break
    fi
    if [[ "${i}" == 60 ]]; then
      echo "PBS did not become ready in time." >&2
      docker logs "${CONTAINER_NAME}" >&2 || true
      exit 1
    fi
    sleep 1
  done

  configure_server

  echo "Waiting for the PBS execution node..."
  for i in {1..60}; do
    if docker exec "${CONTAINER_NAME}" bash -lc \
      'pbsnodes -a pbs 2>/dev/null | grep -q "state = free"'; then
      break
    fi
    if [[ "${i}" == 60 ]]; then
      echo "PBS execution node did not become ready in time." >&2
      docker exec "${CONTAINER_NAME}" pbsnodes -av >&2 || true
      exit 1
    fi
    sleep 1
  done

  smoke_test_apptainer
  docker exec "${CONTAINER_NAME}" qstat -B
  docker exec "${CONTAINER_NAME}" pbsnodes -a
  echo "PBS with Apptainer is ready in ${CONTAINER_NAME}."
}

stop() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}

status() {
  docker exec "${CONTAINER_NAME}" bash -lc 'qstat -B; qstat -a; pbsnodes -a'
}

shell() {
  docker exec -it -u pbsuser "${CONTAINER_NAME}" bash
}

logs() {
  docker logs -f "${CONTAINER_NAME}"
}

client() {
  local command_name="${1:-}"
  local host_cwd container_cwd argument
  local -a translated_args=()
  [[ -n "${command_name}" ]] || { echo "client command is required" >&2; exit 2; }
  shift
  case "${command_name}" in qsub|qstat|qdel|pbsnodes) ;; *) echo "Unsupported PBS client: ${command_name}" >&2; exit 2 ;; esac

  host_cwd="$(cd "${PBS_CLIENT_WORKDIR:-$(pwd -P)}" && pwd -P)"
  if [[ "${host_cwd}" == "${WORKDIR_HOST}" ]]; then
    container_cwd="${WORKDIR_CONTAINER}"
  elif [[ "${host_cwd}" == "${WORKDIR_HOST}"/* ]]; then
    container_cwd="${WORKDIR_CONTAINER}/${host_cwd#"${WORKDIR_HOST}"/}"
  else
    echo "Current directory is outside PBS_WORKDIR_HOST: ${host_cwd}" >&2
    exit 2
  fi
  for argument in "$@"; do
    if [[ "${argument}" == "${WORKDIR_HOST}"/* ]]; then
      translated_args+=("${WORKDIR_CONTAINER}/${argument#"${WORKDIR_HOST}"/}")
    else
      translated_args+=("${argument}")
    fi
  done
  docker exec -i -u pbsuser -w "${container_cwd}" "${CONTAINER_NAME}" \
    bash -lc 'exec "$@"' _ "${command_name}" "${translated_args[@]}"
}

case "${1:-}" in
  build) build ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  shell) shell ;;
  logs) logs ;;
  client) shift; client "$@" ;;
  *) echo "Usage: $0 {build|start|stop|restart|status|shell|logs|client COMMAND}" >&2; exit 2 ;;
esac
