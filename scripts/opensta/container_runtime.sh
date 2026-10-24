#!/usr/bin/env bash
# Separate OpenSTA selection keeps a compile job's Yosys runtime unchanged.
set -euo pipefail
export EMUFLOW_YOSYS_CONTAINER_BINARY="${EMUFLOW_OPENSTA_CONTAINER_BINARY:?set versioned OpenSTA binary}"
if test -n "${EMUFLOW_OPENSTA_CONTAINER_TCL_LIBRARY:-}"; then
  export EMUFLOW_YOSYS_CONTAINER_TCL_LIBRARY="$EMUFLOW_OPENSTA_CONTAINER_TCL_LIBRARY"
fi
task_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for task_launcher in \
  "$task_script_dir/../yosys/container_runtime.sh" \
  "$task_script_dir/../../../../scripts/yosys/container_runtime.sh"
do
  if test -x "$task_launcher"; then
    exec "$task_launcher" "$@"
  fi
done
echo "versioned container runtime launcher is missing" >&2
exit 2
