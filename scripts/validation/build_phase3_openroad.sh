#!/usr/bin/env bash
# Build the modified in-tree solver in a versioned validation directory.
set -euo pipefail
source_dir=$1
build_dir=$2
dependency_root=$3
native_base=$4
jobs=${5:-16}
mkdir -p "$build_dir/tmp"
export TMPDIR="$build_dir/tmp"
cmake -S "$source_dir/engines/openroad" -B "$build_dir/openroad" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$build_dir/install" \
  -DCMAKE_PREFIX_PATH="$dependency_root/deps/spdlog-1.15.0;$dependency_root/deps/source-complete/usr;$dependency_root/tools/ubuntu24/usr;$native_base" \
  -DCUDD_DIR="$native_base" \
  -DOPENROAD_VERSION=a008522d \
  -DUSE_TCL_READLINE=OFF -DENABLE_TESTS=OFF -DBUILD_GUI=OFF \
  -DBUILD_PYTHON=OFF -DBUILD_MAN=OFF \
  -DSWIG_EXECUTABLE="$dependency_root/tools/bin/swig" \
  -DBISON_EXECUTABLE="$dependency_root/tools/bin/bison" \
  -DFLEX_EXECUTABLE="$dependency_root/tools/bin/flex"
cmake --build "$build_dir/openroad" --target openroad --parallel "$jobs"
