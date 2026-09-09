# Yosys source provenance

- Upstream: https://github.com/YosysHQ/yosys
- Release: `v0.57`
- Commit: `3aca86049e79a165932e3e7660358376f45acaed`
- License: ISC (`COPYING`)

The upstream submodules required by this release are materialized as ordinary
source directories so that a clone of this repository is self-contained:

- `abc/`: https://github.com/YosysHQ/abc commit
  `8827bafb7f288de6749dc6e30fa452f2040949c0`
- `libs/cxxopts/`: https://github.com/jarro2783/cxxopts commit
  `4bf61f08697b110d9e3991864650a405b3dd515d`

Yosys also vendors MiniSat source under `libs/minisat/`, with its update script
pointing to https://github.com/niklasso/minisat. That snapshot and its MIT
license are retained exactly as part of the pinned Yosys v0.57 source.

No prebuilt Yosys or ABC executable is stored in this repository. The EmuFlow
top-level build compiles this source tree and the synthesis phase uses that
build product.

Local integration change: `memory_libmap` constructs module connectivity and
the per-memory SAT helper on demand. Mapping costs, SAT queries, and index
invalidation after each emitted memory are unchanged. The memory-library tests
include multiple independent memories to exercise repeated emission.
