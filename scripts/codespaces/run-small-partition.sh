#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if [[ ! -x .venv/bin/emuflow || ! -d build/codespaces-core ]]; then
  scripts/codespaces/bootstrap.sh
fi

export EMUFLOW_NATIVE_ROOT="${repository_root}/build/codespaces-core/install"
export PATH="${repository_root}/.venv/bin:${EMUFLOW_NATIVE_ROOT}/bin:${PATH}"
export PYTHONPATH="${repository_root}/src:${repository_root}"

output_root="${1:-build/codespaces-runs/counter/partition-smoke/attempt-0001}"
if [[ -e "${output_root}" ]]; then
  echo "refusing to overwrite existing output: ${output_root}" >&2
  echo "pass a new output directory for every diagnostic attempt" >&2
  exit 2
fi

platform="platforms/virtual/xcvu3p_2fpga_p2p.json"

emuflow phase1 \
  --yosys-json examples/yosys/counter.json \
  --platform "${platform}" \
  --top counter \
  --clock clk \
  --out "${output_root}/phase1"

for provider in greedy mfspart; do
  emuflow phase3 \
    --ir "${output_root}/phase1/design.emuir.json" \
    --platform "${platform}" \
    --provider "${provider}" \
    --seed 0 \
    --min-used-fpgas 2 \
    --out "${output_root}/phase3-${provider}"
done

for provider in greedy mfspart; do
  echo "${provider}:"
  jq '{provider, seed, validation}' \
    "${output_root}/phase3-${provider}/phase3_report.json"
done

echo "diagnostic output: ${output_root}"
