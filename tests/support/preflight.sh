#!/usr/bin/env bash
# Fail fast before a suite spends hours on an environment problem.
#
#   tests/support/preflight.sh [local|github]
#
# Checks, in order of how often each one has actually bitten:
#   * every registry image the suite pulls is reachable with the current
#     credentials (an expired ghcr.io login in ~/.docker/config.json killed a
#     run 40 minutes in on 2026-09-04, and docker prefers a stale credential
#     over anonymous access, so even public images start failing);
#   * the local image tags the selected mode needs actually exist;
#   * the tools the tests call are installed and the Docker daemon answers;
#   * /storage and the Docker root have room for the run's work directories;
#   * no leftover k3d cluster or PBS/MinIO container from a crashed test.
#
# Exit codes: 0 all good, 1 a hard failure (do not start the suite), 2 usage.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MODE="${1:-local}"
STORAGE_BASE="${PBS_TEST_STORAGE_ROOT:-/storage/github-runner}"
# A full local-build suite consumes roughly 60 GB of Docker storage per run
# (a 4 GB Wasabi client image, a DinD cache per emulation, k3d imports), so a
# 40 GB floor let a run start and then die mid-way: on 2026-09-05 the k3d nodes
# picked up a disk-pressure taint and btc-node never scheduled.
MIN_STORAGE_GB="${PREFLIGHT_MIN_STORAGE_GB:-60}"
MIN_DOCKER_GB="${PREFLIGHT_MIN_DOCKER_GB:-120}"
REGISTRY_TIMEOUT="${PREFLIGHT_REGISTRY_TIMEOUT:-30}"

if [[ "${MODE}" != "local" && "${MODE}" != "github" ]]; then
  echo "usage: $(basename "$0") [local|github]" >&2
  exit 2
fi

failures=0
warnings=0

ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  warn  %s\n' "$1"; warnings=$(( warnings + 1 )); }
fail() { printf '  FAIL  %s\n' "$1"; failures=$(( failures + 1 )); }

echo "Preflight (${MODE} mode)"

# --- tools and daemon ---------------------------------------------------------
for command in docker k3d kubectl python3 timeout jq; do
  if command -v "${command}" >/dev/null 2>&1; then
    ok "command ${command}"
  else
    fail "missing command: ${command}"
  fi
done

if timeout 30 docker info >/dev/null 2>&1; then
  ok "docker daemon reachable"
else
  fail "docker daemon is not reachable"
fi

if [[ "${MODE}" == "local" && -z "${SKIP_LOCAL_IMAGE_BUILD:-}" ]]; then
  emulator_source="${COINJOIN_EMULATOR_SOURCE_DIR:-${PROJECT_DIR}/../coinjoin-emulator}"
  joinmarket_source="${emulator_source}/containers/joinmarket-client-server"
  if [[ -f "${joinmarket_source}/Dockerfile" ]]; then
    ok "local JoinMarket source ${joinmarket_source}"
  else
    fail "missing local JoinMarket source: ${joinmarket_source}"
  fi
fi

# --- registry reachability ----------------------------------------------------
mapfile -t registry_images < <(
  grep -ohE 'ghcr\.io/[a-zA-Z0-9._/-]+(:[A-Za-z0-9._-]+)?' \
    "${PROJECT_DIR}/pipeline/compose.yaml" "${PROJECT_DIR}"/tests/*.sh \
    "${PROJECT_DIR}/container/uploader.image" 2>/dev/null \
  | grep -v '\${' \
  | grep -vE '/$' \
  | grep -v '^ghcr\.io/test/' \
  | sort -u
)
# Each manifest lookup is a network round trip of a few seconds; run them at
# once so the whole preflight stays under ~15s and nobody is tempted to skip it.
registry_results="$(mktemp -d)"
for image in "${registry_images[@]}"; do
  [[ -n "${image}" ]] || continue
  [[ "${image}" == *:* ]] || image="${image}:latest"
  (
    if timeout "${REGISTRY_TIMEOUT}" docker manifest inspect "${image}" >/dev/null 2>&1; then
      echo "ok" > "${registry_results}/$(echo "${image}" | tr '/:' '__')"
    else
      echo "fail" > "${registry_results}/$(echo "${image}" | tr '/:' '__')"
    fi
  ) &
done
wait

for image in "${registry_images[@]}"; do
  [[ -n "${image}" ]] || continue
  [[ "${image}" == *:* ]] || image="${image}:latest"
  result_file="${registry_results}/$(echo "${image}" | tr '/:' '__')"
  if [[ "$(cat "${result_file}" 2>/dev/null)" == "ok" ]]; then
    ok "registry ${image}"
  else
    fail "cannot reach ${image} — check 'docker login ghcr.io' (an expired credential also blocks public images)"
  fi
done
rm -rf "${registry_results}"

# --- published images vs the working tree ---------------------------------------
# The Kubernetes tests pull client images from the registry (they run in k3d,
# which cannot see a locally built image unless it is imported), so a published
# image that predates the working tree means hours of testing code that no longer
# exists. That is exactly how the joinmarket-obwatch defect survived: the fix
# landed on 2026-09-03 and the image in use was built on 2026-08-27.
image_freshness() {  # <package> <repo dir> [path within repo]
  local package="$1" repo="$2" path="${3:-.}"
  command -v gh >/dev/null 2>&1 || return 0
  [[ -d "${repo}/.git" ]] || return 0

  local image_ts
  image_ts="$(gh api "users/ondrejman/packages/container/${package}/versions" \
    --jq 'map(select(.metadata.container.tags | index("latest"))) | .[0].created_at' 2>/dev/null)"
  [[ -n "${image_ts}" && "${image_ts}" != "null" ]] || return 0

  local code_ts
  code_ts="$(git -C "${repo}" log -1 --format=%cI -- "${path}" 2>/dev/null)"
  [[ -n "${code_ts}" ]] || return 0

  local image_epoch code_epoch
  image_epoch="$(date -d "${image_ts}" +%s 2>/dev/null)"
  code_epoch="$(date -d "${code_ts}" +%s 2>/dev/null)"
  [[ -n "${image_epoch}" && -n "${code_epoch}" ]] || return 0

  if (( code_epoch > image_epoch )); then
    local behind
    behind="$(git -C "${repo}" rev-list --count --since="${image_ts}" HEAD -- "${path}" 2>/dev/null)"
    warn "published ${package} was built ${image_ts%T*}, but ${path} changed ${behind:-?} commit(s) since — the Kubernetes tests will run stale code"
  else
    ok "published ${package} is not older than ${path}"
  fi
}

EMULATOR_REPO="${COINJOIN_EMULATOR_SOURCE_DIR:-${PROJECT_DIR}/../coinjoin-emulator}"
# Only the images the Kubernetes tests really pull. In local mode the manager
# image is the locally built coinjoin-emulator:local and test-kubernetes-k3d.sh
# builds and imports its own btc-node, so those two would be false alarms here.
image_freshness joinmarket-client-server "${EMULATOR_REPO}" containers/joinmarket-client-server
image_freshness irc-server "${EMULATOR_REPO}" containers/irc-server
image_freshness btc-node "${EMULATOR_REPO}" containers/btc-node
if [[ "${MODE}" == "github" ]]; then
  image_freshness coinjoin-emulator "${EMULATOR_REPO}" manager
fi

# --- upstream base images -----------------------------------------------------
# The local builds start FROM public bases on Docker Hub and MCR. A transient
# Docker Hub timeout killed the image build one minute into a run on 2026-09-06
# ("failed to resolve source metadata for docker.io/library/python:3.11"), so
# check reachability here rather than after the build step has already failed.
base_images=(
  "python:3.11"
  "debian:bookworm"
  "debian:bookworm-slim"
  "docker:29-dind"
  "docker:29-cli"
  "alpine:latest"
  "mcr.microsoft.com/dotnet/sdk:8.0"
)
# Sequential and best-effort: a burst of anonymous manifest requests trips Docker
# Hub's rate limiter, and a rate-limited probe says "unreachable" about an image
# that builds fine. An image already in the local store needs no registry at all,
# so it is not probed. These are warnings, never blockers — a real outage still
# fails the build with a precise message, and blocking the suite on a throttled
# probe would be worse than the problem.
for image in "${base_images[@]}"; do
  if docker image inspect "${image}" >/dev/null 2>&1; then
    ok "base image ${image} (local)"
    continue
  fi
  if timeout "${REGISTRY_TIMEOUT}" docker manifest inspect "${image}" >/dev/null 2>&1; then
    ok "base image ${image}"
  else
    warn "base image ${image} is neither local nor reachable right now (Docker Hub throttling looks like this too); the build may have to wait for it"
  fi
done

# --- local image tags ---------------------------------------------------------
if [[ "${MODE}" == "local" ]]; then
  for image in \
    "${BLOCKSCI_IMAGE:-blocksci-complete:local}" \
    "${COINJOIN_EMULATOR_IMAGE:-coinjoin-emulator:local}" \
    "${COINJOIN_ANALYSIS_IMAGE:-coinjoin-analysis:local}"; do
    if docker image inspect "${image}" >/dev/null 2>&1; then
      ok "local image ${image}"
    else
      warn "local image ${image} is missing (the build step will create it; --skip-build would fail)"
    fi
  done
fi

# --- disk ---------------------------------------------------------------------
free_gb() { df -BG --output=avail "$1" 2>/dev/null | tail -1 | tr -dc '0-9'; }

if [[ -d "${STORAGE_BASE}" ]]; then
  if [[ -w "${STORAGE_BASE}" ]]; then
    storage_free="$(free_gb "${STORAGE_BASE}")"
    if [[ -n "${storage_free}" ]] && (( storage_free < MIN_STORAGE_GB )); then
      fail "${STORAGE_BASE} has ${storage_free}GB free, below ${MIN_STORAGE_GB}GB"
    else
      ok "${STORAGE_BASE} free: ${storage_free:-?}GB"
    fi
  else
    fail "${STORAGE_BASE} is not writable (the PBS and S3 tests need it)"
  fi
else
  fail "missing ${STORAGE_BASE} (the PBS and S3 tests keep their work roots there)"
fi

docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
if [[ -n "${docker_root}" && -d "${docker_root}" ]]; then
  docker_free="$(free_gb "${docker_root}")"
  if [[ -n "${docker_free}" ]] && (( docker_free < MIN_DOCKER_GB )); then
    reclaimable="$(docker system df --format '{{.Type}} {{.Reclaimable}}' 2>/dev/null | tr '\n' '; ')"
    fail "docker root ${docker_root} has ${docker_free}GB free, below ${MIN_DOCKER_GB}GB"
    printf '        reclaimable: %s\n' "${reclaimable:-unknown}"
    printf '        free it with: docker volume prune -f && docker image prune -f\n'
  else
    ok "docker root free: ${docker_free:-?}GB"
  fi
fi

# --- leftovers ----------------------------------------------------------------
stale_clusters="$(k3d cluster list --no-headers 2>/dev/null \
  | awk '{print $1}' | grep -E '^(coinjoin-k3d|cj-(w|jm)-pbsp?|cj-s3)-[0-9]+$' || true)"
if [[ -n "${stale_clusters}" ]]; then
  warn "leftover k3d cluster(s): $(echo "${stale_clusters}" | tr '\n' ' ')— run scripts/cleanup-stale-clusters.sh"
else
  ok "no leftover k3d clusters"
fi

stale_containers="$(docker ps -a --format '{{.Names}}' 2>/dev/null \
  | grep -E '^(pbs|minio)-[a-z0-9-]*itest-[0-9]+$' || true)"
if [[ -n "${stale_containers}" ]]; then
  warn "leftover test container(s): $(echo "${stale_containers}" | tr '\n' ' ')"
else
  ok "no leftover test containers"
fi

# run-all.sh calls this before its first test, so our own suite is an ancestor,
# not a competitor. Only a suite outside this process tree is a problem.
ancestors=" "
ancestor_pid=$$
while [[ -n "${ancestor_pid}" && "${ancestor_pid}" != "1" ]]; do
  ancestors+="${ancestor_pid} "
  ancestor_pid="$(ps -o ppid= -p "${ancestor_pid}" 2>/dev/null | tr -d ' ')"
done
other_suite=""
for pid in $(pgrep -f 'bash .*run-all\.sh' 2>/dev/null); do
  [[ "${ancestors}" == *" ${pid} "* ]] && continue
  other_suite="${pid}"
  break
done
if [[ -n "${other_suite}" ]]; then
  fail "another suite is already running (pid ${other_suite})"
else
  ok "no competing suite running"
fi

# --- PBS support --------------------------------------------------------------
if [[ -x "${PROJECT_DIR}/tests/support/pbs/local-pbs.sh" && -f "${PROJECT_DIR}/tests/support/pbs/pbs-env.sh" ]]; then
  ok "local PBS support present"
else
  fail "local PBS support missing or not executable under tests/support/pbs/"
fi

echo
if (( failures > 0 )); then
  echo "Preflight FAILED: ${failures} blocking problem(s), ${warnings} warning(s)."
  exit 1
fi
echo "Preflight passed${warnings:+ with ${warnings} warning(s)}."
exit 0
