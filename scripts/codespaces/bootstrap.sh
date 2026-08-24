#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install --editable .

cmake -S . -B build/codespaces-core -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=ON \
  -DEMUFLOW_BUILD_YOSYS=OFF \
  -DEMUFLOW_BUILD_CUDD=OFF \
  -DEMUFLOW_BUILD_REPART=OFF \
  -DEMUFLOW_BUILD_OPENROAD=OFF \
  -DEMUFLOW_BUILD_OPENSTA=OFF \
  -DEMUFLOW_BUILD_FPGA_INTERCHANGE=OFF \
  -DEMUFLOW_BUILD_VTR_ARCHITECTURE=OFF \
  -DEMUFLOW_BUILD_VPR=OFF \
  -DEMUFLOW_BUILD_VPR_PACKED_NETLIST=OFF \
  -DEMUFLOW_BUILD_VPR_ROUTE_CHECKER=OFF \
  -DEMUFLOW_BUILD_OPENPARF=OFF

cmake --build build/codespaces-core --parallel 2

cat <<'EOF'

EmuFlow's partition-focused Codespaces environment is ready.

Start with:
  scripts/codespaces/test-partition.sh smoke
  scripts/codespaces/run-small-partition.sh build/partition-smoke-001

Build the in-tree Yosys frontend only when you are ready to fetch and
synthesize SERV/PicoRV32/AES:
  scripts/codespaces/build-yosys.sh
EOF
