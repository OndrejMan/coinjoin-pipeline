#!/bin/sh
# OpenPBS creates its datastore immediately after `pbs_dataservice start`
# returns, and that only proves the postmaster process exists - not that
# PostgreSQL finished starting up. On a loaded host (the Kubernetes+PBS tests
# build a k3d cluster at the same time) createdb loses the race and the whole
# install aborts with:
#   createdb: error: could not connect to database template1:
#   FATAL:  the database system is starting up
#   Error creating PBS datastore
# Wait for the server to accept connections, then retry a transient createdb.
set -eu

TARGET="${1:-/opt/pbs/libexec/pbs_db_utility}"

python3 - "${TARGET}" <<'PY'
import sys

path = sys.argv[1]
with open(path) as handle:
    source = handle.read()

old = (
    "\terr=`su ${user} -c \"/bin/sh -c '${PGSQL_LIBSTR} ${bin_dir}/createdb"
    " -p ${port} pbs_datastore'\" 2>&1`\n"
    "\n"
    "\tif [ $? -ne 0 ]; then\n"
    '\t\techo "$err"\n'
    '\t\techo "Error creating PBS datastore"\n'
    "\t\t${server_ctl} stop > /dev/null 2>&1\n"
    "\t\tcleanup\n"
    "\t\texit 1\n"
    "\tfi\n"
)

new = (
    "\t# The status check above only proves the postmaster process exists, so\n"
    "\t# wait until PostgreSQL actually accepts connections before creating the\n"
    "\t# datastore, and retry while it is still starting up.\n"
    "\tif [ -x \"${bin_dir}/pg_isready\" ]; then\n"
    "\t\tpbs_ready_tries=${PBS_DATASTORE_READY_TRIES:-60}\n"
    "\t\twhile [ ${pbs_ready_tries} -gt 0 ]; do\n"
    "\t\t\tif su ${user} -c \"/bin/sh -c '${PGSQL_LIBSTR}"
    " ${bin_dir}/pg_isready -q -p ${port}'\" > /dev/null 2>&1; then\n"
    "\t\t\t\tbreak\n"
    "\t\t\tfi\n"
    "\t\t\tpbs_ready_tries=$((pbs_ready_tries-1))\n"
    "\t\t\tsleep 2\n"
    "\t\tdone\n"
    "\tfi\n"
    "\n"
    "\tpbs_createdb_tries=${PBS_DATASTORE_CREATE_TRIES:-30}\n"
    "\twhile :; do\n"
    "\t\terr=`su ${user} -c \"/bin/sh -c '${PGSQL_LIBSTR} ${bin_dir}/createdb"
    " -p ${port} pbs_datastore'\" 2>&1`\n"
    "\t\tpbs_createdb_rc=$?\n"
    "\t\tif [ ${pbs_createdb_rc} -eq 0 ]; then\n"
    "\t\t\tbreak\n"
    "\t\tfi\n"
    "\t\tpbs_createdb_tries=$((pbs_createdb_tries-1))\n"
    "\t\tif [ ${pbs_createdb_tries} -le 0 ]; then\n"
    '\t\t\techo "$err"\n'
    '\t\t\techo "Error creating PBS datastore"\n'
    "\t\t\t${server_ctl} stop > /dev/null 2>&1\n"
    "\t\t\tcleanup\n"
    "\t\t\texit 1\n"
    "\t\tfi\n"
    "\t\tsleep 2\n"
    "\tdone\n"
)

if new in source:
    print(f"{path}: datastore race patch already applied")
    raise SystemExit(0)
if source.count(old) != 1:
    raise SystemExit(
        f"FAIL: expected exactly one createdb block in {path}, "
        f"found {source.count(old)}; OpenPBS changed and the patch needs updating"
    )

with open(path, "w") as handle:
    handle.write(source.replace(old, new))
print(f"{path}: patched createdb to wait for PostgreSQL readiness")
PY

sh -n "${TARGET}"
