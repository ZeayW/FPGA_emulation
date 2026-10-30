#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"
jobs="${EMUFLOW_CODESPACES_JOBS:-2}"

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
  -DEMUFLOW_BUILD_OPENROAD=OFF \
  -DEMUFLOW_BUILD_OPENSTA=OFF \
  -DEMUFLOW_BUILD_FPGA_INTERCHANGE=OFF \
  -DEMUFLOW_BUILD_OPENPARF=OFF

cmake --build build/codespaces-core --parallel "${jobs}"

cat <<'EOF'

EmuFlow's native component development environment is ready.

Run the component checks with:
  scripts/codespaces/test-partition.sh smoke

For a complete flow installation and run, follow README.md and ./install.sh.
EOF
