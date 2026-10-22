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
  ln -s "$binary" "$runtime_dir/bin/$(basename "$binary")"
done
for tool in \
  tlr_router \
  patron_refiner \
  static_exact_analyzer \
  tdm_slot_optimizer \
  tritonpart_balance_legalizer
do
  target="$runtime_dir/bin/emuflow_$tool"
  rm -f "$target"
  if test "$tool" = static_exact_analyzer; then
    "$selected_cxx" -std=c++17 -O3 -pthread \
      -static-libstdc++ -static-libgcc \
      -I"$source_dir/src/native" \
      "$source_dir/src/native/static_exact_analyzer.cpp" \
      "$source_dir/src/native/emuflow_core/artifact_io.cpp" \
      -o "$target"
  else
    "$selected_cxx" -std=c++17 -O3 -pthread \
      -static-libstdc++ -static-libgcc \
      "$source_dir/src/native/$tool.cpp" -o "$target"
  fi
done
printf '%s\n' "$expected_commit" > "$runtime_dir/SOURCE_COMMIT"
printf '%s\n' "$native_base" > "$runtime_dir/NATIVE_BASE_INSTALL"
printf '%s\n' "$source_dir/src" > "$runtime_dir/PYTHON_SOURCE_PATH"
echo RUNTIME_DEPLOYED
