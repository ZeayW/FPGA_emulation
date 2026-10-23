#!/usr/bin/env bash
# Optional --yosys launcher for a versioned binary requiring a newer host ABI.
set -euo pipefail
: "${TMPDIR:?set scratch under the active experiment}"
: "${EMUFLOW_YOSYS_CONTAINER_EXECUTABLE:?set container executable}"
: "${EMUFLOW_YOSYS_CONTAINER_IMAGE:?set immutable image path}"
: "${EMUFLOW_YOSYS_CONTAINER_BINARY:?set versioned Yosys binary}"
: "${EMUFLOW_YOSYS_CONTAINER_BIND:?set input/output filesystem bind}"
: "${EMUFLOW_YOSYS_CONTAINER_LIBRARY_PATH:?set versioned runtime libraries}"
task_scratch="$TMPDIR/yosys-container"
mkdir -p "$task_scratch/cache" "$task_scratch/tmp" "$task_scratch/work"
export SINGULARITY_CACHEDIR="$task_scratch/cache"
export SINGULARITY_TMPDIR="$task_scratch/tmp"
export APPTAINER_CACHEDIR="$task_scratch/cache"
export APPTAINER_TMPDIR="$task_scratch/tmp"
task_environment=("TMPDIR=$TMPDIR" "LD_LIBRARY_PATH=$EMUFLOW_YOSYS_CONTAINER_LIBRARY_PATH")
# OpenSTA extraction passes its sealed artifact paths through this namespace.
# --cleanenv must not drop that data-plane binding.
for task_name in ${!EMUFLOW_STA_@}; do
  task_environment+=("$task_name=${!task_name}")
done
exec "$EMUFLOW_YOSYS_CONTAINER_EXECUTABLE" exec --cleanenv --no-home --contain \
  --workdir "$task_scratch/work" --bind "$EMUFLOW_YOSYS_CONTAINER_BIND" \
  --pwd "$PWD" "$EMUFLOW_YOSYS_CONTAINER_IMAGE" /usr/bin/env \
  "${task_environment[@]}" \
  "$EMUFLOW_YOSYS_CONTAINER_BINARY" "$@"
