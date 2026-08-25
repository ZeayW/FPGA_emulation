#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

jobs="${EMUFLOW_CODESPACES_JOBS:-2}"
python_environment="${repository_root}/.venv-openparf"
python="${python_environment}/bin/python"
build_root="${repository_root}/build/codespaces-physical"

if [[ ! -x "${python}" ]]; then
  python3 -m venv "${python_environment}"
fi

"${python}" -m pip install --disable-pip-version-check --upgrade \
  pip setuptools wheel
"${python}" -m pip install --disable-pip-version-check \
  torch==2.2.2+cpu \
  --index-url https://download.pytorch.org/whl/cpu
"${python}" -m pip install --disable-pip-version-check \
  numpy==1.26.4 \
  PyYAML==6.0.2

"${python}" - <<'PY'
import numpy
import torch
import yaml

assert torch.__version__ == "2.2.2+cpu", torch.__version__
assert numpy.__version__ == "1.26.4", numpy.__version__
assert yaml.__version__ == "6.0.2", yaml.__version__
assert not torch.cuda.is_available()
print(
    "OpenPARF Python dependencies: "
    f"torch={torch.__version__} numpy={numpy.__version__} "
    f"pyyaml={yaml.__version__}"
)
PY

cmake -S . -B "${build_root}" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=ON \
  -DEMUFLOW_EXTERNAL_JOBS="${jobs}" \
  -DEMUFLOW_OPENPARF_PYTHON="${python}" \
  -DEMUFLOW_OPENPARF_ENABLE_CUDA=OFF \
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
  -DEMUFLOW_BUILD_OPENPARF=ON

cmake --build "${build_root}" \
  --target openparf_native \
  --parallel "${jobs}"

# ExternalProject stamps make clean builds reproducible but do not notice an
# edited OpenPARF source file in an existing development tree. Re-enter the
# child build so incremental C++ or Python edits are always installed before
# the regression gate.
cmake --build "${build_root}/openparf" \
  --target install \
  --parallel "${jobs}"

ctest --test-dir "${build_root}" \
  --output-on-failure \
  -R '^openparf_(numerics|singleton_spectral|import|phase2)$'

PYTHONPATH="${build_root}/install/openparf" \
  "${python}" -c \
  'import torch; from openparf.flow import place, route; print(torch.__version__)'

echo "Root-built OpenPARF is available at ${build_root}/install/openparf"
