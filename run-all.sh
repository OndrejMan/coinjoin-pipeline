#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LOCAL_TAG="${LOCAL_TAG:-local}"
LOCAL_WRAPPER_IMAGE="${LOCAL_WRAPPER_IMAGE:-coinjoin-pipeline:${LOCAL_TAG}}"
LOCAL_BLOCKSCI_BASE_IMAGE="${LOCAL_BLOCKSCI_BASE_IMAGE:-blocksci-cj:${LOCAL_TAG}}"
LOCAL_BLOCKSCI_IMAGE="${LOCAL_BLOCKSCI_IMAGE:-blocksci-complete:${LOCAL_TAG}}"
LOCAL_COINJOIN_EMULATOR_IMAGE="${LOCAL_COINJOIN_EMULATOR_IMAGE:-coinjoin-emulator:${LOCAL_TAG}}"
LOCAL_COINJOIN_ANALYSIS_IMAGE="${LOCAL_COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:${LOCAL_TAG}}"

UPSTREAM_WRAPPER_IMAGE="${UPSTREAM_WRAPPER_IMAGE:-ghcr.io/ondrejman/coinjoin-pipeline:latest}"
UPSTREAM_BLOCKSCI_IMAGE="${UPSTREAM_BLOCKSCI_IMAGE:-ghcr.io/ondrejman/blocksci-complete:latest}"
UPSTREAM_COINJOIN_EMULATOR_IMAGE="${UPSTREAM_COINJOIN_EMULATOR_IMAGE:-ghcr.io/ondrejman/coinjoin-emulator:latest}"
UPSTREAM_COINJOIN_ANALYSIS_IMAGE="${UPSTREAM_COINJOIN_ANALYSIS_IMAGE:-ghcr.io/ondrejman/coinjoin-analysis:latest}"
UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX="${UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX:-ghcr.io/ondrejman/}"

EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR:-${SCRIPT_DIR}/emulation_logs}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <local|github> [--build-only] [--pull-only] [--skip-build] [--skip-pull] [--skip-tests] [--tests-only] [--scenario <name-or-path>]...

Run the bundled coinjoin-pipeline workflows with a selected image source.

Modes:
  local                       Build local images and run tests with those tags.
  github                      Pull published GHCR images and run tests with them.

Options:
  --build-only                In local mode, build images and exit.
                              In github mode, pull published images and exit.
  --pull-only                 Pull published images and exit (github mode only).
  --skip-build                Reuse existing local image tags (local mode only).
  --skip-pull                 Do not pre-pull published images (github mode only).
  --skip-tests                Do not run the shell test suite.
  --tests-only                Run tests and no explicit --scenario workflows.
  --scenario <name-or-path>   Run one or more scenario workflows before tests.

Environment overrides:
  LOCAL_TAG                                  Local image tag suffix.
  WRAPPER_IMAGE, BLOCKSCI_IMAGE,            Selected image refs for either mode.
    COINJOIN_EMULATOR_IMAGE,
    COINJOIN_ANALYSIS_IMAGE
  LOCAL_WRAPPER_IMAGE, LOCAL_BLOCKSCI_IMAGE,
    LOCAL_BLOCKSCI_BASE_IMAGE,
    LOCAL_COINJOIN_EMULATOR_IMAGE,
    LOCAL_COINJOIN_ANALYSIS_IMAGE           Local-mode defaults.
  UPSTREAM_WRAPPER_IMAGE, UPSTREAM_BLOCKSCI_IMAGE,
    UPSTREAM_COINJOIN_EMULATOR_IMAGE,
    UPSTREAM_COINJOIN_ANALYSIS_IMAGE        Github-mode defaults.
  UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX   Image prefix for emulator pod images.
  EMULATION_LOGS_DIR                        Output logs directory.
EOF
}

IMAGE_MODE="${1:-}"
if [[ "${IMAGE_MODE}" == "-h" || "${IMAGE_MODE}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${IMAGE_MODE}" == "upstream" ]]; then
  IMAGE_MODE="github"
fi
if [[ "${IMAGE_MODE}" != "local" && "${IMAGE_MODE}" != "github" ]]; then
  echo "ERROR: first argument must be 'local' or 'github'" >&2
  usage >&2
  exit 2
fi
shift

BUILD_IMAGES=""
PULL_IMAGES=""
BUILD_ONLY=0
PULL_ONLY=0
RUN_SCENARIOS=0
RUN_TESTS=1
SCENARIOS=()
CURRENT_CHILD_PID=""
CURRENT_CHILD_PGID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-only)
      BUILD_ONLY=1
      shift
      ;;
    --pull-only)
      PULL_ONLY=1
      shift
      ;;
    --skip-build)
      BUILD_IMAGES=0
      shift
      ;;
    --skip-pull)
      PULL_IMAGES=0
      shift
      ;;
    --skip-tests)
      RUN_TESTS=0
      shift
      ;;
    --tests-only)
      RUN_SCENARIOS=0
      RUN_TESTS=1
      shift
      ;;
    --scenario)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --scenario requires a scenario filename or path" >&2
        exit 2
      fi
      RUN_SCENARIOS=1
      SCENARIOS+=("$2")
      shift 2
      ;;
    --scenario=*)
      RUN_SCENARIOS=1
      SCENARIOS+=("${1#--scenario=}")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${IMAGE_MODE}" == "local" ]]; then
  BUILD_IMAGES="${BUILD_IMAGES:-1}"
  PULL_IMAGES="${PULL_IMAGES:-0}"
  if [[ "${PULL_ONLY}" == "1" ]]; then
    echo "ERROR: --pull-only is only valid in github mode" >&2
    exit 2
  fi
  WRAPPER_IMAGE="${WRAPPER_IMAGE:-${LOCAL_WRAPPER_IMAGE}}"
  BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-${LOCAL_BLOCKSCI_IMAGE}}"
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-${LOCAL_COINJOIN_EMULATOR_IMAGE}}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-${LOCAL_COINJOIN_ANALYSIS_IMAGE}}"
  CHILD_IMAGE_MODE="local"
  BLOCKSCI_PULL_POLICY_VALUE="never"
  COINJOIN_EMULATOR_PULL_POLICY_VALUE="never"
  COINJOIN_ANALYSIS_PULL_POLICY_VALUE="never"
  COINJOIN_EMULATOR_IMAGE_PREFIX_VALUE="${COINJOIN_EMULATOR_IMAGE_PREFIX:-}"
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD_VALUE="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-1}"
else
  BUILD_IMAGES="${BUILD_IMAGES:-0}"
  PULL_IMAGES="${PULL_IMAGES:-1}"
  if [[ "${BUILD_IMAGES}" != "0" ]]; then
    echo "ERROR: github mode does not build local images; use local mode instead" >&2
    exit 2
  fi
  if [[ "${BUILD_ONLY}" == "1" ]]; then
    PULL_ONLY=1
  fi
  WRAPPER_IMAGE="${WRAPPER_IMAGE:-${UPSTREAM_WRAPPER_IMAGE}}"
  BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE:-${UPSTREAM_BLOCKSCI_IMAGE}}"
  COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE:-${UPSTREAM_COINJOIN_EMULATOR_IMAGE}}"
  COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE:-${UPSTREAM_COINJOIN_ANALYSIS_IMAGE}}"
  CHILD_IMAGE_MODE="upstream"
  BLOCKSCI_PULL_POLICY_VALUE="${BLOCKSCI_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_PULL_POLICY_VALUE="${COINJOIN_EMULATOR_PULL_POLICY:-always}"
  COINJOIN_ANALYSIS_PULL_POLICY_VALUE="${COINJOIN_ANALYSIS_PULL_POLICY:-always}"
  COINJOIN_EMULATOR_IMAGE_PREFIX_VALUE="${COINJOIN_EMULATOR_IMAGE_PREFIX:-${UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX}}"
  COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD_VALUE="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD:-}"
fi

if [[ "${RUN_SCENARIOS}" == "1" && "${#SCENARIOS[@]}" -eq 0 ]]; then
  echo "ERROR: --scenario was requested but no scenarios were provided" >&2
  exit 2
fi

if [[ "${BUILD_ONLY}" == "1" || "${PULL_ONLY}" == "1" ]]; then
  RUN_SCENARIOS=0
  RUN_TESTS=0
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 2
  fi
}

handle_interrupt() {
  trap - INT TERM
  echo "Interrupted; stopping current ${IMAGE_MODE} workflow..." >&2

  if [[ -n "${CURRENT_CHILD_PID}" ]] && kill -0 "${CURRENT_CHILD_PID}" >/dev/null 2>&1; then
    if [[ -n "${CURRENT_CHILD_PGID}" ]]; then
      kill -TERM "-${CURRENT_CHILD_PGID}" >/dev/null 2>&1 || true
    else
      kill -TERM "${CURRENT_CHILD_PID}" >/dev/null 2>&1 || true
    fi
    wait "${CURRENT_CHILD_PID}" >/dev/null 2>&1 || true
  fi

  exit 130
}

run_step() {
  set +e
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" &
    CURRENT_CHILD_PID=$!
    CURRENT_CHILD_PGID="${CURRENT_CHILD_PID}"
  else
    "$@" &
    CURRENT_CHILD_PID=$!
    CURRENT_CHILD_PGID=""
  fi

  wait "${CURRENT_CHILD_PID}"
  local status=$?
  CURRENT_CHILD_PID=""
  CURRENT_CHILD_PGID=""
  set -e

  return "${status}"
}

run_in_dir() {
  local workdir="$1"
  shift
  run_step bash -c 'cd "$1"; shift; exec "$@"' _ "${workdir}" "$@"
}

run_with_selected_images_in_dir() {
  local workdir="$1"
  shift
  run_in_dir "${workdir}" env \
    EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
    WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
    BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
    BLOCKSCI_PULL_POLICY="${BLOCKSCI_PULL_POLICY_VALUE}" \
    COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
    COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY_VALUE}" \
    COINJOIN_EMULATOR_IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX_VALUE}" \
    COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD_VALUE}" \
    COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
    COINJOIN_ANALYSIS_PULL_POLICY="${COINJOIN_ANALYSIS_PULL_POLICY_VALUE}" \
    POST_WRAPPER_SHELL=0 \
    BLOCKSCI_LAUNCH_JUPYTER=0 \
    RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS}" \
    "$@"
}

scenario_arg() {
  local scenario="$1"
  if [[ -f "${scenario}" ]]; then
    printf '%s\n' "${scenario}"
  elif [[ -f "${SCRIPT_DIR}/${scenario}" ]]; then
    printf '%s\n' "${scenario}"
  elif [[ -f "${SCRIPT_DIR}/scenarios/${scenario}" ]]; then
    printf '%s\n' "scenarios/${scenario}"
  else
    echo "ERROR: scenario not found: ${scenario}" >&2
    exit 2
  fi
}

engine_for_scenario() {
  local scenario="$1"
  python3 - "$scenario" <<'PY'
import json
import sys
from pathlib import Path

scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if scenario.get("default_version") == "joinmarket":
    print("joinmarket")
else:
    print("wasabi")
PY
}

pull_image() {
  local image="$1"
  if [[ "${image}" != */* ]]; then
    echo "Skipping pull for local image reference ${image}."
    return 0
  fi

  echo "Pulling published image ${image}..."
  run_step docker pull "${image}"
}

verify_blocksci_image() {
  local image="$1"
  echo "Verifying BlockSci runtime in ${image}..."
  run_step docker run --rm --entrypoint /bin/bash "${image}" -lc \
    'command -v blocksci_parser >/dev/null && python3 -c "import blocksci"'
}

if [[ "${RUN_SCENARIOS}" == "1" ]]; then
  require_command python3
fi

if [[ "${RUN_TESTS}" == "1" ]]; then
  require_command uv
fi

if [[ "${BUILD_IMAGES}" == "1" || "${PULL_IMAGES}" == "1" || "${RUN_SCENARIOS}" == "1" || "${RUN_TESTS}" == "1" ]]; then
  require_command docker
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker daemon is not reachable" >&2
    exit 2
  fi
fi

trap handle_interrupt INT TERM

if [[ "${BUILD_IMAGES}" == "1" ]]; then
  echo "Building local BlockSci base image ${LOCAL_BLOCKSCI_BASE_IMAGE}..."
  run_step docker build -t "${LOCAL_BLOCKSCI_BASE_IMAGE}" \
    -f "${REPO_ROOT}/blocksci/Dockerfile" "${REPO_ROOT}/blocksci"

  echo "Building local BlockSci complete image ${BLOCKSCI_IMAGE}..."
  run_step docker build \
    --build-arg "BLOCKSCI_BASE_IMAGE=${LOCAL_BLOCKSCI_BASE_IMAGE}" \
    --build-arg NTHREADS=10 \
    -t "${BLOCKSCI_IMAGE}" \
    -f "${REPO_ROOT}/blocksci/Dockerfile_complete" \
    "${REPO_ROOT}/blocksci"

  echo "Building local CoinJoin emulator image ${COINJOIN_EMULATOR_IMAGE}..."
  run_step docker build -t "${COINJOIN_EMULATOR_IMAGE}" "${REPO_ROOT}/coinjoin-emulator"

  echo "Building local wrapper image ${WRAPPER_IMAGE}..."
  run_step docker build -t "${WRAPPER_IMAGE}" "${SCRIPT_DIR}"

  echo "Building local coinjoin-analysis image ${COINJOIN_ANALYSIS_IMAGE}..."
  run_step docker build -t "${COINJOIN_ANALYSIS_IMAGE}" -f "${REPO_ROOT}/coinjoin-analysis/docker/analysis.Dockerfile" "${REPO_ROOT}/coinjoin-analysis"
fi

if [[ "${IMAGE_MODE}" == "local" ]]; then
  verify_blocksci_image "${BLOCKSCI_IMAGE}"
fi

if [[ "${PULL_IMAGES}" == "1" ]]; then
  pull_image "${WRAPPER_IMAGE}"
  pull_image "${BLOCKSCI_IMAGE}"
  pull_image "${COINJOIN_EMULATOR_IMAGE}"
  pull_image "${COINJOIN_ANALYSIS_IMAGE}"
fi

if [[ "${BUILD_ONLY}" == "1" || "${PULL_ONLY}" == "1" ]]; then
  if [[ "${IMAGE_MODE}" == "local" ]]; then
    echo "Built local images only."
  else
    echo "Pulled published images only."
  fi
  exit 0
fi

mkdir -p "${EMULATION_LOGS_DIR}"

if [[ "${RUN_SCENARIOS}" == "1" ]]; then
  for scenario in "${SCENARIOS[@]}"; do
    resolved_scenario="$(scenario_arg "${scenario}")"
    scenario_path="${resolved_scenario}"
    if [[ "${resolved_scenario}" != /* ]]; then
      scenario_path="${SCRIPT_DIR}/${resolved_scenario}"
    fi

    scenario_engine="$(engine_for_scenario "${scenario_path}")"
    run_args=(full-run --scenario "${resolved_scenario}" --engine "${scenario_engine}")

    echo "Running ${resolved_scenario} with ${IMAGE_MODE} images..."
    run_with_selected_images_in_dir "${SCRIPT_DIR}" bash runIt.sh "${run_args[@]}"
  done
fi

if [[ "${RUN_TESTS}" == "1" ]]; then
  tests=(
    "tests/test-command-builder-contract.sh"
    "tests/pipeline/test_recreate_exit_status.sh"
    "tests/pipeline/test_recreate_interrupt_cleanup.sh"
    "tests/test-runIt-overactive-local.sh"
    "tests/test-podman-no-host-docker.sh"
    "tests/test-runIt-overactive-local-docker.sh"
    "tests/test-runIt-joinmarket-local-docker.sh"
    "tests/test-runIt-parallel-local-docker.sh"
    "tests/test-kubernetes-k3d.sh"
    "tests/test-kubernetes-pbs-analysis.sh"
    "tests/test-parallel-pbs-analysis.sh"
    "tests/test-kubernetes-s3-minio.sh"
  )

  for test_script in "${tests[@]}"; do
    if [[ ! -x "${SCRIPT_DIR}/${test_script}" ]]; then
      echo "ERROR: test script is not executable: ${SCRIPT_DIR}/${test_script}" >&2
      exit 2
    fi

    echo "Running ${test_script} with ${IMAGE_MODE} images..."
    if [[ "${test_script}" == "tests/test-runIt-joinmarket-local-docker.sh" ]]; then
      run_in_dir "${SCRIPT_DIR}" env \
        EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
        LOCAL_WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        LOCAL_BLOCKSCI_BASE_IMAGE="${LOCAL_BLOCKSCI_BASE_IMAGE}" \
        LOCAL_BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        LOCAL_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        LOCAL_COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        UPSTREAM_WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        UPSTREAM_BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        UPSTREAM_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        UPSTREAM_COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD_VALUE}" \
        POST_WRAPPER_SHELL=0 \
        LOCAL_IMAGES_PREBUILT=1 \
        RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS}" \
        bash "${test_script}" "${CHILD_IMAGE_MODE}"
    elif [[ "${test_script}" == "tests/test-kubernetes-pbs-analysis.sh" ]]; then
      # PBS executes analyzer images inside Apptainer, which cannot see host
      # Docker tags. In github mode the analyzers stay registry-backed docker://
      # references (MetaCentrum parity); in local mode the test exports the
      # freshly built analyzer images as docker-archive tarballs so the whole
      # suite runs offline against local code.
      pbs_local_image_env=()
      if [[ "${IMAGE_MODE}" == "local" ]]; then
        pbs_local_image_env=(
          "PBS_BLOCKSCI_LOCAL_IMAGE=${BLOCKSCI_IMAGE}"
          "PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE=${COINJOIN_ANALYSIS_IMAGE}"
        )
      fi
      run_in_dir "${SCRIPT_DIR}" env \
        EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
        WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY_VALUE}" \
        BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        "${pbs_local_image_env[@]}" \
        IMAGE_PREFIX="${UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX}" \
        COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD= \
        bash "${test_script}" all
    elif [[ "${test_script}" == "tests/test-parallel-pbs-analysis.sh" ]]; then
      # Same image constraints as the serial Kubernetes+PBS test: registry-backed
      # docker:// analyzer references in github mode, docker-archive exports of
      # the local analyzer images in local mode.
      pbs_local_image_env=()
      if [[ "${IMAGE_MODE}" == "local" ]]; then
        pbs_local_image_env=(
          "PBS_BLOCKSCI_LOCAL_IMAGE=${BLOCKSCI_IMAGE}"
          "PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE=${COINJOIN_ANALYSIS_IMAGE}"
        )
      fi
      run_in_dir "${SCRIPT_DIR}" env \
        EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
        WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        COINJOIN_EMULATOR_PULL_POLICY="${COINJOIN_EMULATOR_PULL_POLICY_VALUE}" \
        BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        "${pbs_local_image_env[@]}" \
        IMAGE_PREFIX="${UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX}" \
        COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD= \
        bash "${test_script}" all
    elif [[ "${test_script}" == "tests/test-kubernetes-s3-minio.sh" ]]; then
      # The S3-compatible e2e imports the selected wrapper/emulator images into
      # k3d. In local mode, analyzer images are exported as docker-archive
      # tarballs because Apptainer cannot see host Docker tags. Supporting
      # Wasabi infrastructure images still use the published image prefix.
      pbs_local_image_env=()
      if [[ "${IMAGE_MODE}" == "local" ]]; then
        pbs_local_image_env=(
          "PBS_BLOCKSCI_LOCAL_IMAGE=${BLOCKSCI_IMAGE}"
          "PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE=${COINJOIN_ANALYSIS_IMAGE}"
        )
      fi
      run_in_dir "${SCRIPT_DIR}" env \
        WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        "${pbs_local_image_env[@]}" \
        IMAGE_PREFIX="${UPSTREAM_COINJOIN_EMULATOR_IMAGE_PREFIX}" \
        bash "${test_script}" wasabi
    elif [[ "${test_script}" == "tests/test-kubernetes-k3d.sh" ]]; then
      run_in_dir "${SCRIPT_DIR}" env \
        WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        IMAGE_PREFIX="${COINJOIN_EMULATOR_IMAGE_PREFIX_VALUE}" \
        EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
        COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD="${COINJOIN_EMULATOR_INFRASTRUCTURE_LOCAL_BUILD_VALUE}" \
        POST_WRAPPER_SHELL=0 \
        RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS}" \
        bash "${test_script}"
    elif [[ "${test_script}" == "tests/test-runIt-overactive-local-docker.sh" ]]; then
      run_in_dir "${SCRIPT_DIR}" env \
        EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
        LOCAL_TAG="${LOCAL_TAG}" \
        LOCAL_WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        LOCAL_BLOCKSCI_BASE_IMAGE="${LOCAL_BLOCKSCI_BASE_IMAGE}" \
        LOCAL_BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        LOCAL_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        LOCAL_COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        POST_WRAPPER_SHELL=0 \
        LOCAL_IMAGES_PREBUILT=1 \
        RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS}" \
        bash "${test_script}" "${CHILD_IMAGE_MODE}"
    elif [[ "${test_script}" == "tests/test-runIt-parallel-local-docker.sh" ]]; then
      run_in_dir "${SCRIPT_DIR}" env \
        EMULATION_LOGS_DIR="${EMULATION_LOGS_DIR}" \
        LOCAL_TAG="${LOCAL_TAG}" \
        LOCAL_WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        LOCAL_BLOCKSCI_BASE_IMAGE="${LOCAL_BLOCKSCI_BASE_IMAGE}" \
        LOCAL_BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        LOCAL_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        LOCAL_COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        WRAPPER_IMAGE="${WRAPPER_IMAGE}" \
        BLOCKSCI_IMAGE="${BLOCKSCI_IMAGE}" \
        COINJOIN_EMULATOR_IMAGE="${COINJOIN_EMULATOR_IMAGE}" \
        COINJOIN_ANALYSIS_IMAGE="${COINJOIN_ANALYSIS_IMAGE}" \
        POST_WRAPPER_SHELL=0 \
        LOCAL_IMAGES_PREBUILT=1 \
        RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS}" \
        bash "${test_script}" "${CHILD_IMAGE_MODE}"
    else
      run_with_selected_images_in_dir "${SCRIPT_DIR}" bash "${test_script}"
    fi
  done
fi

completed_parts=()
if [[ "${RUN_SCENARIOS}" == "1" ]]; then
  completed_parts+=("${#SCENARIOS[@]} ${IMAGE_MODE} coinjoin-pipeline run(s)")
fi
if [[ "${RUN_TESTS}" == "1" ]]; then
  completed_parts+=("all coinjoin-pipeline shell tests")
fi

if [[ "${#completed_parts[@]}" -eq 0 ]]; then
  if [[ "${BUILD_IMAGES}" == "1" ]]; then
    completion_summary="local image build"
  elif [[ "${PULL_IMAGES}" == "1" ]]; then
    completion_summary="published image pull"
  else
    completion_summary="no workflows"
  fi
elif [[ "${#completed_parts[@]}" -eq 1 ]]; then
  completion_summary="${completed_parts[0]}"
else
  completion_summary="${completed_parts[0]}"
  for ((i = 1; i < ${#completed_parts[@]}; i++)); do
    completion_summary+=", ${completed_parts[$i]}"
  done
fi

echo "PASS: completed ${completion_summary}. Logs: ${EMULATION_LOGS_DIR}"
