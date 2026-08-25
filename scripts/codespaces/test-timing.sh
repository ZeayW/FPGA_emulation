#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if [[ ! -x .venv/bin/emuflow || ! -d build/codespaces-core ]]; then
  scripts/codespaces/bootstrap.sh
fi
if [[ ! -x build/codespaces-opensta/install/bin/sta ]]; then
  scripts/codespaces/build-opensta.sh
fi

export EMUFLOW_NATIVE_ROOT="${repository_root}/build/codespaces-core/install"
export PATH="${repository_root}/.venv/bin:${EMUFLOW_NATIVE_ROOT}/bin:${PATH}"
export PYTHONPATH="${repository_root}/src:${repository_root}"

output_root="${1:-build/codespaces-runs/counter/timing-smoke/attempt-0001}"
if [[ -e "${output_root}" ]]; then
  echo "refusing to overwrite existing output: ${output_root}" >&2
  echo "pass a new output directory for every diagnostic attempt" >&2
  exit 2
fi

emuflow phase1 \
  --yosys-json examples/yosys/counter.json \
  --top counter \
  --clock clk \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --out "${output_root}/phase1"

emuflow sta run-opensta \
  --ir "${output_root}/phase1/design.emuir.json" \
  --clock-period clk=10 \
  --opensta build/codespaces-opensta/install/bin/sta \
  --log "${output_root}/opensta.log" \
  --output "${output_root}/path-database.json"

emuflow sta validate-path-database \
  --database "${output_root}/path-database.json" \
  --ir "${output_root}/phase1/design.emuir.json"

emuflow sta derive-partition-net-weights \
  --database "${output_root}/path-database.json" \
  --ir "${output_root}/phase1/design.emuir.json" \
  --output "${output_root}/partition-net-weights.json"

jq -e '
  .schema == "emuflow.sta-path-database/v1"
  and .design == "counter"
  and (.paths | length) > 0
' "${output_root}/path-database.json" >/dev/null

echo "OpenSTA timing smoke passed: ${output_root}"
