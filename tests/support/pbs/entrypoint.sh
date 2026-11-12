#!/usr/bin/env bash
set -euo pipefail

cat >/etc/pbs.conf <<EOF
PBS_EXEC=/opt/pbs
PBS_SERVER=${PBS_HOSTNAME:-pbs}
PBS_START_SERVER=1
PBS_START_SCHED=1
PBS_START_COMM=1
PBS_START_MOM=${PBS_START_MOM:-1}
PBS_HOME=/var/spool/pbs
PBS_CORE_LIMIT=unlimited
PBS_SCP=/usr/bin/scp
EOF

mkdir -p /var/spool/pbs /scratch
chmod 1777 /scratch

/opt/pbs/libexec/pbs_postinstall
install -d -m 0775 -o postgres -g postgres /var/run/postgresql

# The first start also creates the PostgreSQL-backed datastore, which is the
# fragile part on a loaded host. A failed attempt leaves nothing behind (PBS
# removes the half-built datastore itself), so retrying is safe and beats
# losing the whole container - and the test run with it - to one slow start.
dump_dataservice_log() {
  local log
  for log in /var/spool/pbs/datastore/pg_log/*; do
    [[ -f "${log}" ]] || continue
    echo "===== ${log} (last 50 lines) =====" >&2
    tail -n 50 "${log}" >&2 || true
  done
}

attempt=1
until /opt/pbs/libexec/pbs_init.d start; do
  if (( attempt >= ${PBS_INIT_ATTEMPTS:-3} )); then
    echo "PBS did not start after ${attempt} attempts." >&2
    dump_dataservice_log
    exit 1
  fi
  echo "PBS start attempt ${attempt} failed; retrying..." >&2
  dump_dataservice_log
  /opt/pbs/libexec/pbs_init.d stop >/dev/null 2>&1 || true
  attempt=$((attempt + 1))
  sleep 5
done

exec "$@"
