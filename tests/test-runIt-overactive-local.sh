#!/usr/bin/env bash
# Public launcher smoke test with no runtime or daemon access.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
export EMULATION_LOGS_DIR="$TMP_DIR/runs"
mkdir -p "$TMP_DIR/bin"
cat > "$TMP_DIR/bin/docker" <<'FAKE'
#!/bin/sh
echo 'dry-run unexpectedly contacted Docker' >&2
exit 99
FAKE
chmod +x "$TMP_DIR/bin/docker"
export PATH="$TMP_DIR/bin:$PATH"
"$PROJECT_DIR/runIt.sh" full-run --engine wasabi --dry-run > "$TMP_DIR/output"
grep -Fq '[dry-run] action: full-run' "$TMP_DIR/output"
test ! -e "$EMULATION_LOGS_DIR"
cat > "$TMP_DIR/run.yaml" <<'YAML'
action: full-run
engine: joinmarket
YAML
(cd "$TMP_DIR" && "$PROJECT_DIR/runIt.sh" run "$TMP_DIR/run.yaml" --dry-run) > "$TMP_DIR/output"
grep -Fq '[dry-run] engine: joinmarket' "$TMP_DIR/output"
test ! -e "$EMULATION_LOGS_DIR"
if "$PROJECT_DIR/runIt.sh" analyze --dry-run > "$TMP_DIR/output" 2>&1; then
  echo 'FAIL: analyze accepted no run directory' >&2
  exit 1
fi
grep -Fq 'requires --run-dir' "$TMP_DIR/output"
echo 'PASS: public CLI loads configuration without a wrapper process or runtime access.'
