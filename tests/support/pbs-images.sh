# shellcheck shell=bash
# Run a PBS analyzer from a local Docker image instead of a registry-backed
# docker:// reference. Apptainer cannot see host Docker tags, so the image is
# exported into the shared workspace with `docker save` and executed as a
# docker-archive reference.
#
#   source tests/support/pbs-images.sh
#   export_pbs_docker_archive <image> <archive name> <cjp image flag>
#
# The caller sets WORK_ROOT and PBS_IMAGE_ARGS=(); each call appends the flag
# and its docker-archive reference to PBS_IMAGE_ARGS.

export_pbs_docker_archive() {
  local image="$1" archive_name="$2" image_flag="$3"
  docker image inspect "${image}" >/dev/null 2>&1 || {
    echo "FAIL: local image for PBS docker-archive export not found: ${image}" >&2
    exit 2
  }
  mkdir -p "${WORK_ROOT}/pbs-images"
  echo "Exporting ${image} as ${archive_name}.tar for offline PBS execution..."
  docker save "${image}" -o "${WORK_ROOT}/pbs-images/${archive_name}.tar"
  chmod 0644 "${WORK_ROOT}/pbs-images/${archive_name}.tar"
  PBS_IMAGE_ARGS+=("${image_flag}" "docker-archive:${WORK_ROOT}/pbs-images/${archive_name}.tar")
}
