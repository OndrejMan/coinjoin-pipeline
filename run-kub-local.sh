#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

exec sudo -E env \
  COINJOIN_EMULATOR_IMAGE=coinjoin-emulator:local \
  BLOCKSCI_IMAGE=blocksci-complete:local \
  COINJOIN_ANALYSIS_IMAGE=coinjoin-analysis:local \
  PBS_BLOCKSCI_LOCAL_IMAGE=blocksci-complete:local \
  PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE=coinjoin-analysis:local \
  IMAGE_PREFIX=ghcr.io/ondrejman/ \
  bash tests/test-kubernetes-s3-minio.sh wasabi
