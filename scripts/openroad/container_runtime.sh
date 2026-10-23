#!/usr/bin/env bash
# Keep OpenROAD's binary selection independent of Yosys in one compile job.
set -euo pipefail
export EMUFLOW_YOSYS_CONTAINER_BINARY="${EMUFLOW_OPENROAD_CONTAINER_BINARY:?set versioned OpenROAD binary}"
if test -n "${EMUFLOW_OPENROAD_CONTAINER_TCL_LIBRARY:-}"; then
  export EMUFLOW_YOSYS_CONTAINER_TCL_LIBRARY="$EMUFLOW_OPENROAD_CONTAINER_TCL_LIBRARY"
fi
task_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$task_script_dir/../yosys/container_runtime.sh" "$@"
