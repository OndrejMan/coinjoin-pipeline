#!/usr/bin/env bash
# Bounded real-mainnet variant of the shared local PBS/MinIO synchronization E2E.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
  exec bash "$repo/tests/test-mainnet-sync-local.sh" --help
fi
[[ $# == 0 ]] || { echo 'Use MAINNET_TEST_TARGET_HEIGHT to choose the bounded height.' >&2; exit 2; }
exec bash "$repo/tests/test-mainnet-sync-local.sh" mainnet
