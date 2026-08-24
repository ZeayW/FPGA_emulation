#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repository_root}"

if [[ ! -x .venv/bin/python || ! -d build/codespaces-core ]]; then
  scripts/codespaces/bootstrap.sh
fi

export EMUFLOW_NATIVE_ROOT="${repository_root}/build/codespaces-core/install"
export PATH="${repository_root}/.venv/bin:${EMUFLOW_NATIVE_ROOT}/bin:${PATH}"
export PYTHONPATH="${repository_root}/src:${repository_root}"

suite="${1:-smoke}"

case "${suite}" in
  smoke)
    modules=(
      tests.test_phase1
      tests.test_phase3
      tests.test_mfspart_phase3
    )
    ;;
  partition)
    modules=(
      tests.test_phase1
      tests.test_phase3
      tests.test_tritonpart
      tests.test_mfspart
      tests.test_mfspart_initial
      tests.test_mfspart_refine
      tests.test_mfspart_legalize
      tests.test_mfspart_phase3
      tests.test_partition_hops
      tests.test_partition_feedback
      tests.test_cross_stage
    )
    ;;
  all)
    exec ctest --test-dir build/codespaces-core --output-on-failure
    ;;
  *)
    echo "usage: $0 [smoke|partition|all]" >&2
    exit 2
    ;;
esac

.venv/bin/python -m unittest -v "${modules[@]}"
