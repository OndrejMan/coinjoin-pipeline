#!/bin/sh
set -eu

daemon_pid=""

cleanup() {
  if [ -n "${daemon_pid}" ] && kill -0 "${daemon_pid}" 2>/dev/null; then
    kill "${daemon_pid}" 2>/dev/null || true
    wait "${daemon_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

/usr/local/bin/dockerd-entrypoint.sh dockerd --host=unix:///var/run/docker.sock >/var/log/dockerd.log 2>&1 &
daemon_pid=$!

attempt=0
until docker info >/dev/null 2>&1; do
  if ! kill -0 "${daemon_pid}" 2>/dev/null; then
    cat /var/log/dockerd.log >&2 || true
    echo "ERROR: embedded Docker daemon exited before becoming ready." >&2
    exit 5
  fi
  attempt=$((attempt + 1))
  if [ "${attempt}" -ge 60 ]; then
    cat /var/log/dockerd.log >&2 || true
    echo "ERROR: embedded Docker daemon did not become ready within 60 seconds." >&2
    exit 5
  fi
  sleep 1
done

exec /workspace/coinjoin-pipeline/container/launcher.sh "$@"
