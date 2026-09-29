#!/usr/bin/env bash
# Build benchmark tools with optimization; an empty CMake build type is -O0.
set -euo pipefail
cd "$(dirname "$0")/.."
source_dir="${1:-../yosys}"
cmake -S "$source_dir" -B build/tools/yosys-build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PWD/build/tools/yosys" \
  -DYOSYS_ENABLE_UNIT_TESTS=OFF
cmake --build build/tools/yosys-build --parallel "${BUILD_JOBS:-4}"
cmake --install build/tools/yosys-build
