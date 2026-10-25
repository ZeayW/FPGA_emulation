# OpenPARF source provenance

- Upstream: https://github.com/PKU-IDEA/OpenPARF
- Imported commit: `793cac2bb109e5cc76046f87d39ed70fa093cf60`
- Upstream license: BSD-3-Clause (`LICENSE`)

This directory contains the OpenPARF build source used by EmuFlow, including
the small upstream build-time benchmark directory. Generated build products,
precompiled extension modules, and Git-LFS unit-test datasets are not
included. The root build configures OpenPARF with `BUILD_TESTING=OFF`.

The imported source includes the checked EmuFlow changes recorded in:

- `../../patches/openparf/export-global-placement.patch`
- `../../scripts/openparf/patches/torch-public-dispatch.patch`
- `../../scripts/openparf/patches/torch-fft-api.patch`
- `../../scripts/openparf/patches/torch-fft-shape.patch`

The vendored `openparf/placement/placer.py` also contains a small
source-visible PyTorch API compatibility change: deterministic mode uses
`torch.use_deterministic_algorithms` when available and retains the legacy
fallback for older PyTorch releases.

The SLL multiplier update retains its native `[0,1000)` acceptance rule and
uses a log-domain comparison when the exponential step would overflow.
Oversize positive updates remain rejected and negative updates still clamp to
zero; finite ordinary updates retain the original arithmetic.

EmuFlow also adds a source-visible generic packed-cluster mode. It makes the
logic area-type names architecture-configurable and uses OpenPARF's existing
single-site min-cost-flow legalizer for VTR-defined clusters instead of
requiring Xilinx-specific `LUT` and `FF` area types. The native LUT/FF path is
unchanged when this mode is disabled.

OpenPARF's upstream Git submodules are vendored as ordinary source
directories so a clone of EmuFlow does not need recursive submodule checkout:

| Path | Upstream | Revision |
| --- | --- | --- |
| `cmake/Ccache.cmake` | https://github.com/TheLartians/Ccache.cmake | `2f890b8d3bd810442c482797c24c7b3d97755215` |
| `thirdparty/blend2d` | https://github.com/blend2d/blend2d | `405bcc3b57524b9a6b11cfeecf99c01fe34839dc` |
| `thirdparty/googletest` | https://github.com/google/googletest | `bc860af08783b8113005ca7697da5f5d49a8056f` |
| `thirdparty/pugixml` | https://github.com/zeux/pugixml | `80a531ee1dc13269d148fd0b1cedbcd7352f7c68` |
| `thirdparty/pybind11` | https://github.com/pybind/pybind11 | `f70165463328c218d118204efc13aac93783d17b` |
| `thirdparty/yaml-cpp` | https://github.com/jbeder/yaml-cpp | `0e6e28d1a38224fc8172fae0109ea7f673c096db` |

Each vendored tree retains its upstream license. The root build defaults to
CPU PyTorch operators for portability. CUDA remains a source build option
when a PyTorch/CUDA toolkit pair is explicitly supplied.

OpenPARF also contains directly vendored LEMON 1.3.1
(https://github.com/The-OpenROAD-Project/lemon-graph) and rapidcsv 8.65
(https://github.com/d99kris/rapidcsv). The disabled experimental router
contains additional nested source; every path and upstream link is listed in
`../../OPEN_SOURCE_COMPONENTS.md`.

OpenPARF's optional `fpga-router` subtree is excluded from the default
placement build because it currently requires the proprietary GUROBI solver.
The source remains vendored for provenance, but
`EMUFLOW_OPENPARF_BUILD_EXPERIMENTAL_ROUTER` defaults to `OFF`. Enabling that
option is an explicitly non-open experimental configuration and must not
satisfy a source-complete release gate. EmuFlow's system-level router is a
separate in-tree component; an open detailed FPGA-routing backend remains a
distinct integration milestone.

The sparse-direct-legalizer experiment remains an optional source patch and is
not part of the default monorepo build.

The Python density initialization uses target-density capacity when budgeting
fillers. This matches the existing target-scaled fixed/background charge and
overflow operator. Filler geometry retains the half-bin sampling bound; the
EmuFlow driver independently validates the initial area budget before solving.

ISM receives the configured routing/pin capacities and rejects nonpositive or
nonfinite values. Its three matching stages reset demand maps before kernel
accumulation and normalize pin demand by pin-bin area. This avoids mixing
previous normalized utilization into fresh demand. A checked operator-only
CMake build can replace ISM without modifying the base runtime.
The integration's fixed-mask extension reserves occupied matching sites before
creating fillers. Every matching stage checks fixed coordinates and duplicate
LUT/FF slots, including collisions between movable clusters and fixed macros.

Large router example netlists are omitted because they are input data rather
than implementation source.
