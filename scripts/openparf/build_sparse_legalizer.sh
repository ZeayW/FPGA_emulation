#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 OPENPARF_SOURCE OPENPARF_BUILD OFFICIAL_INSTALL ALTERNATE_INSTALL [JOBS]" >&2
  exit 2
fi

source_dir="$(cd -- "$1" && pwd)"
build_dir="$(cd -- "$2" && pwd)"
official_install="$(cd -- "$3" && pwd)"
alternate_install="$4"
jobs="${5:-8}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"
patch_file="$repo_root/patches/openparf/sparse-greedy-first.patch"
solver="$source_dir/openparf/ops/direct_lg/src/dl_solver.cpp"
solver_backup="$solver.emuflow-backup.$$"
alternate_parent="$(dirname -- "$alternate_install")"
alternate_name="$(basename -- "$alternate_install")"
staging="$alternate_parent/.${alternate_name}.tmp.$$"

test -f "$patch_file"
test -f "$solver"
test -d "$official_install/openparf/ops/direct_lg"
mkdir -p "$alternate_parent"

if ! grep -q \
  'if (!ripupLegalizeInst(inst, _param.nbrDistEnd) && !greedyLegalizeInst(inst))' \
  "$solver"; then
  echo "OpenPARF direct legalizer does not match the expected upstream source" >&2
  exit 1
fi

restore_source() {
  if [[ -f "$solver_backup" ]]; then
    cp -p -- "$solver_backup" "$solver"
    rm -f -- "$solver_backup"
  fi
  rm -f -- "$solver.rej"
}
cleanup_staging() {
  rm -rf -- "$staging"
}
trap 'restore_source; cleanup_staging' EXIT

patch --silent --dry-run --directory "$source_dir" -p1 < "$patch_file"
cp -p -- "$solver" "$solver_backup"
patch --silent --directory "$source_dir" -p1 < "$patch_file"
rm -rf -- "$staging"
cp -a -- "$official_install" "$staging"
cmake --build "$build_dir" --target direct_lg_cpp -- -j"$jobs"
cp "$build_dir/openparf/ops/direct_lg"/direct_lg_cpp*.so \
  "$staging/openparf/ops/direct_lg/"
rm -rf -- "$alternate_install"
mv -- "$staging" "$alternate_install"

restore_source
grep -q \
  'if (!ripupLegalizeInst(inst, _param.nbrDistEnd) && !greedyLegalizeInst(inst))' \
  "$solver"
trap - EXIT

echo "EMUFLOW_OPENPARF_SPARSE_LEGALIZER status=pass install=$alternate_install"
