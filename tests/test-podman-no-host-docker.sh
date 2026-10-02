#!/usr/bin/env bash
# Probe and launch only the selected runtime through fake commands.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/bin"
export PODMAN_LOG="$TMP_DIR/podman.log" DOCKER_LOG="$TMP_DIR/docker.log"
cat > "$TMP_DIR/bin/docker" <<'FAKE'
#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
exit 99
FAKE
cat > "$TMP_DIR/bin/podman" <<'FAKE'
#!/bin/sh
echo "$*" >> "$PODMAN_LOG"
exit 0
FAKE
chmod +x "$TMP_DIR/bin/"*
export PATH="$TMP_DIR/bin:$PATH" EMULATION_LOGS_DIR="$TMP_DIR/runs"
"$PROJECT_DIR/runIt.sh" --runtime podman doctor > "$TMP_DIR/doctor"
grep -q '^info' "$PODMAN_LOG"
grep -q '^image inspect' "$PODMAN_LOG"
"$PROJECT_DIR/runIt.sh" container podman emulate --engine wasabi --dry-run > "$TMP_DIR/dry-run"
grep -Fq '[dry-run] runtime: podman' "$TMP_DIR/dry-run"
test ! -e "$DOCKER_LOG"
echo 'PASS: Podman selection and preflight never use the host Docker daemon.'
