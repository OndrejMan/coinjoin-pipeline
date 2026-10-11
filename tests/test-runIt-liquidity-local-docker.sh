#!/usr/bin/env bash
# Postmix spends end to end: the liquidity-postmix-smoke scenario has two Wasabi
# wallets pay part of their mixed coins out of the pool through the real
# client RPC. The run must confirm both spends and every analyzer must see
# value leaving the mix. Arguments and environment as in
# test-runIt-overactive-local-docker.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

E2E_SCENARIO_FILE="scenarios/liquidity-postmix-smoke.json" \
E2E_SCENARIO_NAME="liquidity-postmix-smoke" \
E2E_LIQUIDITY_CHECKS="--require-postmix" \
  exec bash "${SCRIPT_DIR}/test-runIt-overactive-local-docker.sh" "$@"
