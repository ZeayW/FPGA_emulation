#!/usr/bin/env bash
# Keep the PATRON refiner selection independent of Yosys in one compile job.
set -euo pipefail
export EMUFLOW_YOSYS_CONTAINER_BINARY="${EMUFLOW_PATRON_CONTAINER_BINARY:?set versioned PATRON binary}"
task_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$task_script_dir/../yosys/container_runtime.sh" "$@"
