#!/usr/bin/env bash
# Build the emulator's container images from the working tree and put them into
# a k3d cluster, so a Kubernetes test exercises the code in this checkout rather
# than whatever was last published to the registry.
#
#   source tests/support/local-images.sh
#   local_images_build <engine> [scenario-file]   # before the cluster exists
#   local_images_import <cluster>                 # after `k3d cluster create`
#
# k3d nodes keep their own containerd store, so a host-built image is invisible
# until `k3d image import` copies it in; that is why these tests pulled from the
# registry in the first place. A published image that lags the working tree is
# silent: the joinmarket-obwatch defect was fixed on 2026-09-03 and still failed
# every run because the image in use was built on 2026-08-27.
#
# Missing sources, failed builds and failed imports stop the test so it cannot
# silently exercise a published image instead of the working tree.
# Set SKIP_LOCAL_IMAGE_BUILD=1 to force the published images for a whole run.

LOCAL_IMAGES_BUILT=()

_local_images_emulator_dir() {
  echo "${COINJOIN_EMULATOR_SOURCE_DIR:-${PROJECT_DIR}/../coinjoin-emulator}"
}

_local_images_wasabi_contexts() {  # <scenario-file>
  local scenario="${1:-}"
  python3 - "${scenario}" "${WASABI_VERSION:-2.6.0}" <<'PY'
import json
import sys

scenario = {}
if sys.argv[1]:
    with open(sys.argv[1]) as handle:
        scenario = json.load(handle)
versions = {scenario.get("default_version") or sys.argv[2]}
if scenario.get("distributor_version"):
    versions.add(scenario["distributor_version"])
versions.update(wallet["version"] for wallet in scenario.get("wallets", []) if wallet.get("version"))
for version in sorted(versions):
    print(f"wasabi-client:{version} wasabi-clients/{version}")
# Match the emulator's backend architecture selection across all client versions.
if any(version >= "2.6.0" for version in versions):
    print("wasabi-backend:2.6.0 wasabi-backend/2.6.0")
    print("wasabi-coordinator:2.6.0 wasabi-coordinator/2.6.0")
else:
    print("wasabi-backend:2.0.4 wasabi-backend/2.0.4")
PY
}

_local_images_build_one() {  # <tag> <context>
  local tag="$1" context="$2"
  if [[ ! -f "${context}/Dockerfile" ]]; then
    echo "FAIL: no Dockerfile in ${context} for ${tag}" >&2
    return 1
  fi
  echo "Building ${tag} from ${context}"
  if docker build -t "${tag}" "${context}" >/dev/null; then
    LOCAL_IMAGES_BUILT+=("${tag}")
  else
    echo "FAIL: build of ${tag} failed" >&2
    return 1
  fi
}

local_images_build() {  # <engine> [scenario-file]
  local engine="${1:-wasabi}" scenario="${2:-}"
  LOCAL_IMAGES_BUILT=()
  [[ -z "${SKIP_LOCAL_IMAGE_BUILD:-}" ]] || { echo "SKIP_LOCAL_IMAGE_BUILD=1; using published images"; return 0; }
  local emulator_dir prefix
  emulator_dir="$(_local_images_emulator_dir)"
  prefix="${IMAGE_PREFIX:-ghcr.io/ondrejman/}"
  if [[ ! -d "${emulator_dir}/containers" ]]; then
    echo "FAIL: emulator sources not found at ${emulator_dir}" >&2
    return 1
  fi

  # test-kubernetes-k3d.sh builds and imports its own btc-node under a
  # cluster-specific tag; do not build a second copy behind its back.
  if [[ -z "${COINJOIN_BTC_NODE_IMAGE:-}" ]]; then
    _local_images_build_one "${prefix}btc-node:latest" "${emulator_dir}/containers/btc-node" || return 1
  fi

  if [[ "${engine}" == "joinmarket" ]]; then
    _local_images_build_one "${prefix}joinmarket-client-server:latest" \
      "${emulator_dir}/containers/joinmarket-client-server" || return 1
    _local_images_build_one "${prefix}irc-server:latest" \
      "${emulator_dir}/containers/irc-server" || return 1
    return 0
  fi

  local contexts image context
  contexts="$(_local_images_wasabi_contexts "${scenario}")" || return 1
  while read -r image context; do
    _local_images_build_one "${prefix}${image}" "${emulator_dir}/containers/${context}" || return 1
  done <<< "${contexts}"
}

local_images_import() {  # <cluster>
  local cluster="$1"
  (( ${#LOCAL_IMAGES_BUILT[@]} > 0 )) || return 0
  local imported=()
  local image
  for image in "${LOCAL_IMAGES_BUILT[@]}"; do
    if k3d image import --cluster "${cluster}" "${image}" >/dev/null; then
      imported+=("${image}")
    else
      echo "FAIL: k3d image import failed for ${image}" >&2
      return 1
    fi
  done
  if (( ${#imported[@]} > 0 )); then
    echo "Imported working-tree images into ${cluster}: ${imported[*]}"
    # Without this the pods would pull the published image over the import.
    export KUBERNETES_IMAGE_PULL_POLICY=IfNotPresent
  fi
}
