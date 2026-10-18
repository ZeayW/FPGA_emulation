#!/usr/bin/env bash
# Thin, versioned runtime: exact pushed Python source plus rebuilt changed tools.
set -euo pipefail
source_dir=$1
runtime_dir=$2
native_base=$3
expected_commit=$4
# The minimal physical runtime image has no Git. The host verifies clean
# pushed provenance before entering it; retain a fail-closed HEAD check here.
if command -v git >/dev/null 2>&1; then
  actual_commit=$(git --git-dir="$source_dir/.git" rev-parse HEAD)
  test -z "$(git --git-dir="$source_dir/.git" --work-tree="$source_dir" status --porcelain)"
else
  IFS= read -r actual_commit < "$source_dir/.git/HEAD"
  case "$actual_commit" in
    'ref: '*) IFS= read -r actual_commit < "$source_dir/.git/${actual_commit#ref: }" ;;
  esac
fi
test "$actual_commit" = "$expected_commit"
case "$runtime_dir" in /research/d4/gds/ziyiwang21/*) ;; *) exit 2;; esac
mkdir -p "$runtime_dir/bin" "$runtime_dir/tmp"
export TMPDIR="$runtime_dir/tmp"
for binary in "$native_base"/bin/*; do
  test -e "$binary" || continue
  ln -s "$binary" "$runtime_dir/bin/$(basename "$binary")"
done
for tool in \
  tlr_router \
  patron_refiner \
  tdm_slot_optimizer \
  tritonpart_balance_legalizer
do
  target="$runtime_dir/bin/emuflow_$tool"
  rm -f "$target"
  c++ -std=c++17 -O3 -pthread "$source_dir/src/native/$tool.cpp" -o "$target"
done
printf '%s\n' "$expected_commit" > "$runtime_dir/SOURCE_COMMIT"
printf '%s\n' "$native_base" > "$runtime_dir/NATIVE_BASE_INSTALL"
printf '%s\n' "$source_dir/src" > "$runtime_dir/PYTHON_SOURCE_PATH"
echo RUNTIME_DEPLOYED
