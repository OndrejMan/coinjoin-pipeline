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
/opt/pbs/libexec/pbs_init.d start

exec "$@"
