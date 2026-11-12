# shellcheck shell=bash
# Shared by the local runIt workflows (test-runIt-*-local-docker.sh and
# test-runIt-overactive-local-podman.sh).
#
#   source tests/support/local-runit.sh
#   list_run_dirs                          # run directories below LOGS_DIR, sorted
#   build_local_images <docker|podman>     # the analyzer images from the sibling checkouts
#   stop_blocksci                          # stop the BlockSci container of COMPOSE_PROJECT
#   wait_for_runit <name> <stop command>   # wait for the runIt.sh backgrounded as RUN_PID
#
# The caller sets LOGS_DIR, RUN_LOG, RUN_PID, RUN_TIMEOUT_SECONDS, PROJECT_DIR,
# COMPOSE_PROJECT, the LOCAL_*_IMAGE tags and the *_SOURCE_DIR checkouts.

list_run_dirs() {
  find "${LOGS_DIR}" -mindepth 1 -maxdepth 1 -type d \
    -exec test -s "{}/coinjoin_emulator_data/scenario.json" \; -print |
    LC_ALL=C sort
}

build_local_images() {  # <docker|podman>
  local tool="$1"
  echo "Building local BlockSci base image ${LOCAL_BLOCKSCI_BASE_IMAGE} from ${BLOCKSCI_SOURCE_DIR}..."
  "${tool}" build -t "${LOCAL_BLOCKSCI_BASE_IMAGE}" -f "${BLOCKSCI_SOURCE_DIR}/Dockerfile" "${BLOCKSCI_SOURCE_DIR}"

  echo "Building local BlockSci complete image ${LOCAL_BLOCKSCI_IMAGE} from ${BLOCKSCI_SOURCE_DIR}..."
  "${tool}" build \
    --build-arg "BLOCKSCI_BASE_IMAGE=${LOCAL_BLOCKSCI_BASE_IMAGE}" \
    -t "${LOCAL_BLOCKSCI_IMAGE}" \
    -f "${BLOCKSCI_SOURCE_DIR}/Dockerfile_complete" \
    "${BLOCKSCI_SOURCE_DIR}"

  echo "Building local CoinJoin emulator image ${LOCAL_EMULATOR_IMAGE} from ${EMULATOR_SOURCE_DIR}..."
  "${tool}" build -t "${LOCAL_EMULATOR_IMAGE}" "${EMULATOR_SOURCE_DIR}"

  echo "Building local coinjoin-analysis image ${LOCAL_COINJOIN_ANALYSIS_IMAGE} from ${COINJOIN_ANALYSIS_SOURCE_DIR}..."
  "${tool}" build -t "${LOCAL_COINJOIN_ANALYSIS_IMAGE}" \
    -f "${COINJOIN_ANALYSIS_SOURCE_DIR}/docker/analysis.Dockerfile" "${COINJOIN_ANALYSIS_SOURCE_DIR}"
}

stop_blocksci() {
  docker compose -f "${PROJECT_DIR}/pipeline/compose.yaml" -p "${COMPOSE_PROJECT}" \
    stop blocksci >/dev/null 2>&1 || true
}

# Fails the test when the run opens the BlockSci Jupyter server (it must stay
# noninteractive), outlives RUN_TIMEOUT_SECONDS or exits nonzero.
wait_for_runit() {  # <name of the run in messages> <stop command>
  local name="$1" stop="$2" elapsed status run_finished=false
  for ((elapsed = 0; elapsed < RUN_TIMEOUT_SECONDS; elapsed++)); do
    if ! kill -0 "${RUN_PID}" >/dev/null 2>&1; then
      run_finished=true
      break
    fi

    if grep -q "Use Control-C to stop this server" "${RUN_LOG}" ||
      grep -q "http://127.0.0.1:8888/tree?token" "${RUN_LOG}"; then
      echo "FAIL: noninteractive ${name} launched the BlockSci Jupyter server" >&2
      echo "Expected BLOCKSCI_LAUNCH_JUPYTER=0 to make analysis exit after exports." >&2
      "${stop}"
      wait "${RUN_PID}" >/dev/null 2>&1 || true
      RUN_PID=""
      exit 1
    fi

    sleep 1
  done

  if [[ "${run_finished}" == "false" ]] && kill -0 "${RUN_PID}" >/dev/null 2>&1; then
    echo "FAIL: ${name} did not exit within ${RUN_TIMEOUT_SECONDS}s" >&2
    "${stop}"
    wait "${RUN_PID}" >/dev/null 2>&1 || true
    RUN_PID=""
    exit 1
  fi

  set +e
  wait "${RUN_PID}"
  status=$?
  set -e
  RUN_PID=""
  if [[ "${status}" -ne 0 ]]; then
    echo "FAIL: ${name} exited with code ${status}" >&2
    exit "${status}"
  fi
}
