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

# The login environment on the validation farm still exposes GCC 7 as `c++`.
# That compiler accepts -std=c++17 but does not provide <filesystem>, which is
# required by emuflow_core/artifact_io.cpp.  The farm's system libstdc++ is also
# older than the GCC 14 compiler installation.  Select a compiler by capability
# instead of trusting PATH or a version string, and link the small native tools
# with their compiler's C++ runtime so they do not inherit that ABI mismatch.
# CXX, when provided, remains an explicit fail-closed override and must name one
# executable.
filesystem_probe_source="$runtime_dir/tmp/cxx17_filesystem_probe.cpp"
filesystem_probe_binary="$runtime_dir/tmp/cxx17_filesystem_probe"
cleanup_filesystem_probe() {
  rm -f "$filesystem_probe_source" "$filesystem_probe_binary"
}
trap cleanup_filesystem_probe EXIT
cat > "$filesystem_probe_source" <<'EOF'
#include <filesystem>
int main() {
  const std::filesystem::path path{"."};
  return path.empty();
}
EOF

selected_cxx=
if test -n "${CXX:-}"; then
  if cxx_path=$(command -v "$CXX" 2>/dev/null) && \
      "$cxx_path" -std=c++17 -static-libstdc++ -static-libgcc \
        "$filesystem_probe_source" \
        -o "$filesystem_probe_binary" >/dev/null 2>&1 && \
      "$filesystem_probe_binary"; then
    selected_cxx=$cxx_path
  else
    echo "CXX does not compile and run the required C++17 <filesystem> probe: $CXX" >&2
    exit 2
  fi
else
  for cxx_candidate in \
    /usr/local/gcc/bin/g++ \
    /research/d4/gds/ziyiwang21/usr/local/gcc-10.1.0/bin/g++ \
    c++ \
    g++
  do
    if cxx_path=$(command -v "$cxx_candidate" 2>/dev/null) && \
        "$cxx_path" -std=c++17 -static-libstdc++ -static-libgcc \
          "$filesystem_probe_source" \
          -o "$filesystem_probe_binary" >/dev/null 2>&1 && \
        "$filesystem_probe_binary"; then
      selected_cxx=$cxx_path
      break
    fi
  done
fi
test -n "$selected_cxx" || {
  echo "no compiler passed the required C++17 <filesystem> probe" >&2
  exit 2
}
rm -f "$filesystem_probe_source" "$filesystem_probe_binary"
trap - EXIT
echo "RUNTIME_CXX=$selected_cxx"
"$selected_cxx" --version

for binary in "$native_base"/bin/*; do
  test -e "$binary" || continue
  # Retired provider binaries must not leak in through an older native base.
  if test "$(basename "$binary")" = emuflow_tritonpart_balance_legalizer; then
    continue
  fi
  ln -s "$binary" "$runtime_dir/bin/$(basename "$binary")"
done
sqlite_flags=()
if test -n "${SQLITE3_PREFIX:-}"; then
  sqlite_flags=("-I$SQLITE3_PREFIX/include" "-L$SQLITE3_PREFIX/lib"
    "-Wl,-rpath,$SQLITE3_PREFIX/lib")
fi
for tool in \
  tlr_router \
  patron_refiner \
  static_exact_analyzer \
  tdm_exact_scheduler \
  tdm_ratio_optimizer \
  tdm_slot_optimizer \
  tdm_timing_dag_optimizer
do
  target="$runtime_dir/bin/emuflow_$tool"
  rm -f "$target"
  "$selected_cxx" -std=c++17 -O3 -pthread \
    -static-libstdc++ -static-libgcc \
    "${sqlite_flags[@]}" -I"$source_dir/src/native" \
    "$source_dir/src/native/$tool.cpp" \
    "$source_dir/src/native/emuflow_core/artifact_io.cpp" \
    "$source_dir/src/native/emuflow_core/schedule_model.cpp" \
    "$source_dir/src/native/emuflow_core/sqlite.cpp" \
    "$source_dir/src/native/emuflow_core/stage_runtime.cpp" \
    "$source_dir/src/native/emuflow_core/emuir_view.cpp" \
    -lsqlite3 -o "$target"
done

# Production adapters prefer the unified sibling whenever it exists.  A thin
# runtime must therefore rebuild that entry from the same exact source commit;
# retaining the native base's older emuflow-native while refreshing only the
# standalone binaries would silently execute stale stage code.
unified="$runtime_dir/bin/emuflow-native"
rm -f "$unified"
"$selected_cxx" -std=c++17 -O3 -pthread \
  -static-libstdc++ -static-libgcc \
  -DEMUFLOW_NATIVE_STAGE_LIBRARY=1 \
  "${sqlite_flags[@]}" -I"$source_dir/src/native" \
  "$source_dir/src/native/emuflow_native.cpp" \
  "$source_dir/src/native/emuflow_core/native_pipeline.cpp" \
  "$source_dir/src/native/emuflow_core/schedule_store.cpp" \
  "$source_dir/src/native/emuflow_core/route_view.cpp" \
  "$source_dir/src/native/emuflow_core/schedule_model.cpp" \
  "$source_dir/src/native/emuflow_core/schedule_ingress.cpp" \
  "$source_dir/src/native/emuflow_core/tool_runtime.cpp" \
  "$source_dir/src/native/emuflow_core/compile.cpp" \
  "$source_dir/src/native/patron_refiner.cpp" \
  "$source_dir/src/native/static_exact_analyzer.cpp" \
  "$source_dir/src/native/tlr_router.cpp" \
  "$source_dir/src/native/tdm_exact_scheduler.cpp" \
  "$source_dir/src/native/chimew_bank_channel_assigner.cpp" \
  "$source_dir/src/native/chimew_rudy.cpp" \
  "$source_dir/src/native/chimew_signal_grouper.cpp" \
  "$source_dir/src/native/chimew_position_refiner.cpp" \
  "$source_dir/src/native/emuflow_core/artifact_io.cpp" \
  "$source_dir/src/native/emuflow_core/sqlite.cpp" \
  "$source_dir/src/native/emuflow_core/stage_runtime.cpp" \
  "$source_dir/src/native/emuflow_core/emuir_view.cpp" \
  -lsqlite3 -o "$unified"
target="$runtime_dir/bin/emuflow_xilinx_bookshelf_exporter"
rm -f "$target"
"$selected_cxx" -std=c++17 -O3 -static-libstdc++ -static-libgcc \
  "${sqlite_flags[@]}" \
  "$source_dir/src/native/xilinx_bookshelf_exporter.cpp" -lsqlite3 -o "$target"
# Do not publish a runtime whose unified core or physical bridge cannot even
# load its linked dependencies in the deployment environment. The run launcher
# must repeat these probes inside its actual execution environment.
"$unified" check-runtime >/dev/null
"$target" --help >/dev/null
printf '%s\n' "$expected_commit" > "$runtime_dir/SOURCE_COMMIT"
printf '%s\n' "$native_base" > "$runtime_dir/NATIVE_BASE_INSTALL"
printf '%s\n' "$source_dir/src" > "$runtime_dir/PYTHON_SOURCE_PATH"
echo RUNTIME_DEPLOYED
