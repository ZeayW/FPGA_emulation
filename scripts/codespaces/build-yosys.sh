#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

cmake -S . -B build/codespaces-yosys -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=ON \
  -DEMUFLOW_BUILD_YOSYS=ON \
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

cmake --build build/codespaces-yosys --parallel 2

echo "Yosys is available at build/codespaces-yosys/install/bin/yosys"
