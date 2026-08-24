#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if [[ ! -x .venv/bin/emuflow || ! -d build/codespaces-core ]]; then
  scripts/codespaces/bootstrap.sh
fi
if [[ ! -x build/codespaces-yosys/install/bin/yosys ]]; then
  scripts/codespaces/build-yosys.sh
fi
if [[ ! -x build/codespaces-opensta/install/bin/sta ]]; then
  scripts/codespaces/build-opensta.sh
fi

export EMUFLOW_NATIVE_ROOT="${repository_root}/build/codespaces-core/install"
export PATH="${repository_root}/.venv/bin:${EMUFLOW_NATIVE_ROOT}/bin:${PATH}"
export PYTHONPATH="${repository_root}/src:${repository_root}"

output_root="${1:-build/codespaces-runs/serv/timing-flow/attempt-0001}"
if [[ -e "${output_root}" ]]; then
  echo "refusing to overwrite existing output: ${output_root}" >&2
  echo "pass a new output directory for every diagnostic attempt" >&2
  exit 2
fi

python3 scripts/benchmarks/fetch.py fetch serv

emuflow benchmark benchmarks/runs/serv_l1.json \
  --source-root third_party/rtl/serv \
  --yosys build/codespaces-yosys/install/bin/yosys \
  --out "${output_root}/benchmark"

emuflow multi-fpga compile \
  --yosys-json "${output_root}/benchmark/synthesis/mapped.json" \
  --top serv_synth_wrapper \
  --clock clk \
  --clock-period clk=10 \
  --platform platforms/virtual/xcvu3p_2fpga_p2p.json \
  --opensta build/codespaces-opensta/install/bin/sta \
  --partition-provider mfspart \
  --min-used-fpgas 2 \
  --equivalence-cycles 4 \
  --out "${output_root}/flow"

report="${output_root}/flow/multi-fpga-flow-report.json"
jq -e '
  .status == "pass"
  and .summary.status == "pass"
  and .timing.status == "pass"
  and .timing.backend == "opensta"
  and .timing.optimization_enabled == true
  and .stages.frontend.status == "pass"
  and .stages.partition.status == "pass"
  and .stages.partition.validation.status == "pass"
  and .stages.system_route.status == "pass"
  and .stages.system_route.validation.status == "pass"
  and .stages.tdm.status == "pass"
  and .stages.tdm.validation.status == "pass"
  and .stages.split.status == "pass"
  and .stages.split.validation.status == "pass"
' "${report}" >/dev/null

jq '{
  status,
  summary,
  timing: {
    status: .timing.status,
    backend: .timing.backend,
    optimization_enabled: .timing.optimization_enabled,
    paths: .timing.sta.paths,
    worst_slack_ns: .timing.sta.checker.worst_slack_ns
  },
  partition: {
    provider: .stages.partition.provider,
    cut_nets: .stages.partition.validation.cut_nets,
    balance_auto_relaxed: .stages.partition.validation.balance_auto_relaxed,
    requested_balance_percent:
      .stages.partition.validation.requested_balance_percent,
    effective_balance_percent:
      .stages.partition.validation.effective_balance_percent
  },
  route: {
    provider: .stages.system_route.provider,
    max_link_utilization:
      .stages.system_route.validation.max_link_utilization
  },
  tdm: {
    provider: .stages.tdm.provider,
    completion_slot: .stages.tdm.validation.completion_slot,
    collisions: .stages.tdm.validation.collisions
  }
}' "${report}"

if jq -e '.stages.partition.validation.balance_auto_relaxed == true' \
  "${report}" >/dev/null; then
  cat <<'EOF'
WARNING: Phase 3 automatically relaxed the requested balance tolerance.
This run is a functional timing-flow diagnostic, not fair partitioning QoR
evidence.  Use a naturally capacity-constrained design and a feasible fixed
balance contract before comparing partition providers.
EOF
fi

echo "SERV timing-driven diagnostic passed: ${output_root}"
