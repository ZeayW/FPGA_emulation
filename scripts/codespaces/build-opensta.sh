#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"
jobs="${EMUFLOW_CODESPACES_JOBS:-2}"

cmake -S . -B build/codespaces-opensta -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=ON \
  -DEMUFLOW_EXTERNAL_JOBS="${jobs}" \
  -DEMUFLOW_BUILD_YOSYS=OFF \
  -DEMUFLOW_BUILD_CUDD=ON \
  -DEMUFLOW_BUILD_REPART=OFF \
  -DEMUFLOW_BUILD_OPENROAD=OFF \
  -DEMUFLOW_BUILD_OPENSTA=ON \
  -DEMUFLOW_BUILD_FPGA_INTERCHANGE=OFF \
  -DEMUFLOW_BUILD_VTR_ARCHITECTURE=OFF \
  -DEMUFLOW_BUILD_VPR=OFF \
  -DEMUFLOW_BUILD_VPR_PACKED_NETLIST=OFF \
  -DEMUFLOW_BUILD_VPR_ROUTE_CHECKER=OFF \
  -DEMUFLOW_BUILD_OPENPARF=OFF

cmake --build build/codespaces-opensta \
  --target opensta_native \
  --parallel "${jobs}"

echo "OpenSTA is available at build/codespaces-opensta/install/bin/sta"
