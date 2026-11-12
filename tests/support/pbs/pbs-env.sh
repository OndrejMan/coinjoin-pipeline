#!/usr/bin/env bash

# Source this file to use the exact PBS client installed in the local server
# container as ordinary host commands.
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _pbs_env_source="${BASH_SOURCE[0]}"
elif [[ -n "${ZSH_VERSION:-}" ]]; then
  _pbs_env_source="${(%):-%N}"
else
  echo "Cannot determine pbs-env.sh location for this shell." >&2
  return 2
fi
_pbs_scripts_dir="$(cd "$(dirname "${_pbs_env_source}")" && pwd)"
export PBS_WORKDIR_HOST="${PBS_WORKDIR_HOST:-/storage/github-runner}"
export PBS_WORKDIR_CONTAINER="${PBS_WORKDIR_CONTAINER:-${PBS_WORKDIR_HOST}}"
export PBS_CONTAINER_NAME="${PBS_CONTAINER_NAME:-pbs}"
export PATH="${_pbs_scripts_dir}/pbs-bin:${PATH}"
unset _pbs_env_source _pbs_scripts_dir
