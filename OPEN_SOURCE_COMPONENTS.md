# Open-source components and provenance

This file is the central human-readable source map for EmuFlow. It covers
source copied into this repository, source-built flow engines, pinned open
architecture data, direct external build/runtime dependencies, CI actions,
and fetchable RTL benchmarks. Exact
machine-readable records are in
[`OPEN_SOURCE_COMPONENTS.json`](OPEN_SOURCE_COMPONENTS.json), while
[`SOURCE_MANIFEST.json`](SOURCE_MANIFEST.json) maps implementations to flow
stages and build targets.

The license file retained beside each imported tree is authoritative. An
EmuFlow commit does not relicense third-party code.

## First-party source

EmuFlow's own control plane, C++ optimization kernels, RTL, schemas, scripts,
tests, and documentation originate in this repository:
[ZeayW/FGPA_emulation](https://github.com/ZeayW/FGPA_emulation). They are
covered by the root Apache-2.0 `LICENSE`; imported trees listed below retain
their own licenses.

## Source present in this repository

| Local path | Component and upstream source | Pinned revision/version | License | Use |
| --- | --- | --- | --- | --- |
| `engines/corundum_eth` | Minimal [Corundum/verilog-ethernet](https://github.com/corundum/corundum/tree/1ca0151b97af85aa5dd306d74b6bcec65904d2ce/fpga/lib/eth) source closure | `1ca0151b97af85aa5dd306d74b6bcec65904d2ce` | MIT | Source-visible 10GBASE-R PCS and Gray-pointer asynchronous FIFO; imported files are hash-pinned and unmodified |
| `providers/vivado_gty_10g` | Adapted [verilog-ethernet VCU108 10G GTY recipe](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/VCU108/fpga_10g/ip/eth_xcvr_gt.tcl) plus the EmuFlow v3 quad adapter | `77320a9471d19c7dd383914bc049e02d9f4f1ffb` | MIT | Source-visible 64+2 adapter and recipe; Vivado-generated XCI products stay outside source control and do not count as the open flow |
| `engines/capnproto` | [Cap'n Proto](https://github.com/capnproto/capnproto) | `373e61ec89e2359f1c362e9b2eadc552f4779306` / v1.5.0 | Apache-2.0 | Build and parse FPGA Interchange messages |
| `engines/fpga-interchange-schema` | [FPGA Interchange Schema](https://github.com/chipsalliance/fpga-interchange-schema) | `c985b4648e66414b250261c1ba4cbe45a2971b1c` | Apache-2.0 | Open logical-netlist and device-resource contracts |
| `engines/fpga-interchange-schema/third_party/capnproto-java` | [capnproto-java schema](https://github.com/capnproto/capnproto-java) | `1a0ac9d2e0e607ccae7ca83cb3aacce93b065dd7` | MIT | Vendored `java.capnp` import required by the schema |
| `engines/cudd` | [CUDD](https://github.com/ivmai/cudd) | `f54f533303640afd5dbe47a05ebeabb3066f2a25` / 3.0.0 | BSD-3-Clause | OpenSTA decision diagrams |
| `engines/yosys` | [Yosys](https://github.com/YosysHQ/yosys) | `3aca86049e79a165932e3e7660358376f45acaed` / v0.57 | ISC | RTL synthesis |
| `engines/yosys/abc` | [YosysHQ ABC](https://github.com/YosysHQ/abc) | `8827bafb7f288de6749dc6e30fa452f2040949c0` | ABC upstream notices | Logic optimization and mapping |
| `engines/yosys/libs/cxxopts` | [cxxopts](https://github.com/jarro2783/cxxopts) | `4bf61f08697b110d9e3991864650a405b3dd515d` | MIT | Yosys command-line parsing |
| `engines/yosys/libs/minisat` | [MiniSat](https://github.com/niklasso/minisat) | Yosys v0.57 vendored snapshot | MIT | Yosys SAT solving |
| `engines/repart` | [RePart](https://github.com/Welement-zyf/RePart) | `211a9d8fd526576387cad7ac6dd3531354aeb31c` | GPL-3.0-only | Multilevel hypergraph partitioning |
| `engines/openroad` | [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD) | `a008522d88b669ac4c985609533cf5a3d2649222` | BSD-3-Clause | TritonPart and timing infrastructure |
| `engines/openroad/src/sta` | [OpenSTA](https://github.com/The-OpenROAD-Project/OpenSTA) | `aa598a2f14c5c142e90391a69988523505e7db3d` | GPL-3.0-or-later | Static timing analysis |
| `engines/openroad/third-party/abc` | [OpenROAD ABC](https://github.com/The-OpenROAD-Project/abc) | `ef5389d31526003c2ebd7e6d6d6fe3848a20f0a2` | ABC upstream notices | OpenROAD logic functions |
| `engines/openroad/src/grt/src/fastroute` | [FastRoute source in OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD/tree/a008522d88b669ac4c985609533cf5a3d2649222/src/grt/src/fastroute) | OpenROAD pinned snapshot | BSD-3-Clause | OpenROAD global routing support |
| `engines/openroad/src/stt/src/flt` | [Flute3 source in OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD/tree/a008522d88b669ac4c985609533cf5a3d2649222/src/stt/src/flt) | OpenROAD pinned snapshot | BSD-3-Clause | Rectilinear Steiner trees |
| `engines/openroad/src/ppl/src/munkres` | [Munkres source in OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD/tree/a008522d88b669ac4c985609533cf5a3d2649222/src/ppl/src/munkres) | OpenROAD pinned snapshot | BSD-2-Clause | Assignment optimization |
| `engines/openroad/src/gui/resources/google_icons` | [Google Material Design Icons](https://github.com/google/material-design-icons) | OpenROAD pinned asset snapshot | Apache-2.0 | OpenROAD GUI build resources |
| `engines/openparf` | [OpenPARF](https://github.com/PKU-IDEA/OpenPARF) | `793cac2bb109e5cc76046f87d39ed70fa093cf60` | BSD-3-Clause | FPGA placement |
| `engines/openparf/cmake/Ccache.cmake` | [Ccache.cmake](https://github.com/TheLartians/Ccache.cmake) | `2f890b8d3bd810442c482797c24c7b3d97755215` | MIT | Optional compiler-cache integration |
| `engines/openparf/thirdparty/blend2d` | [Blend2D](https://github.com/blend2d/blend2d) | `405bcc3b57524b9a6b11cfeecf99c01fe34839dc` | zlib | Placement drawing |
| `engines/openparf/thirdparty/googletest` | [GoogleTest](https://github.com/google/googletest) | `bc860af08783b8113005ca7697da5f5d49a8056f` | BSD-3-Clause | Upstream tests; EmuFlow disables them |
| `engines/openparf/thirdparty/pugixml` | [pugixml](https://github.com/zeux/pugixml) | `80a531ee1dc13269d148fd0b1cedbcd7352f7c68` | MIT | XML parsing |
| `engines/openparf/thirdparty/pybind11` | [pybind11](https://github.com/pybind/pybind11) | `f70165463328c218d118204efc13aac93783d17b` | BSD-3-Clause | Python/C++ bindings |
| `engines/openparf/thirdparty/yaml-cpp` | [yaml-cpp](https://github.com/jbeder/yaml-cpp) | `0e6e28d1a38224fc8172fae0109ea7f673c096db` | MIT | Architecture/configuration parsing |
| `engines/openparf/thirdparty/yaml-cpp/test/gtest-1.11.0` | [GoogleTest](https://github.com/google/googletest/tree/release-1.11.0) | 1.11.0 upstream test snapshot | BSD-3-Clause | yaml-cpp upstream tests; disabled |
| `engines/openparf/thirdparty/lemon` | [LEMON graph library](https://github.com/The-OpenROAD-Project/lemon-graph) | 1.3.1 | BSL-1.0 | Graph algorithms |
| `engines/openparf/thirdparty/rapidcsv` | [rapidcsv](https://github.com/d99kris/rapidcsv) | 8.65 | BSD-3-Clause | CSV parsing |
| `engines/vtr` | [Verilog-to-Routing / VPR](https://github.com/verilog-to-routing/vtr-verilog-to-routing) | `a3e60c31bb4384373d2d1a43c38c7569723733b6` | MIT | Exact architecture packing, routing-resource graph construction, baseline placement, and detailed routing |
| `engines/vtr/libs/EXTERNAL/libpugixml` | [pugixml](https://github.com/zeux/pugixml) | 1.7 | MIT | VPR and EmuFlow packed-netlist XML parsing |
| `engines/vtr/libs/EXTERNAL/libsdcparse` | [libsdcparse](https://github.com/verilog-to-routing/libsdcparse) | `7a49e2c9ad469d314aa7ec07d3e893ecabd7d9dc` | MIT | VPR SDC parsing |
| `engines/vtr/libs/EXTERNAL/yaml-cpp` | [yaml-cpp](https://github.com/jbeder/yaml-cpp) | `2decf96e915d2b0c26c68c1659665789dfef2633` | MIT | VPR YAML report/configuration parsing |

Yosys ABC and OpenROAD ABC each carry additional SAT solver, compression, and
BDD code. Those copies originate in their respective pinned ABC repositories;
their per-directory license files remain in place. OpenROAD also retains
module-level license files throughout `engines/openroad/src`.

## Retained but disabled OpenPARF router source

OpenPARF's experimental detailed router is present for source provenance but
is not part of the default build because it requires proprietary GUROBI.
EmuFlow does not count it as an open detailed-routing backend.

| Local path | Component and upstream source | Version | License |
| --- | --- | --- | --- |
| `engines/openparf/openparf/routing/fpga-router` | [OpenPARF router snapshot](https://github.com/PKU-IDEA/OpenPARF/tree/793cac2bb109e5cc76046f87d39ed70fa093cf60/openparf/routing/fpga-router) | OpenPARF pinned snapshot | OpenPARF notices |
| `.../3rdparty/clipp` | [clipp](https://github.com/muellan/clipp) | OpenPARF router snapshot | MIT |
| `.../3rdparty/gdstk` | [gdstk](https://github.com/heitzmann/gdstk) | 0.4.0 | BSL-1.0 |
| `.../3rdparty/gdstk/gdstk/libqhull_r` | [Qhull](https://github.com/qhull/qhull) | gdstk bundled snapshot | Qhull license |
| `.../3rdparty/gdstk/gdstk/clipperlib` | [Clipper](https://sourceforge.net/projects/polyclipping/) | 6.4.2 | BSL-1.0 |
| `.../3rdparty/lemon` | [LEMON graph library](https://github.com/The-OpenROAD-Project/lemon-graph) | 1.3.1 | BSL-1.0 |
| `.../3rdparty/pugixml` | [pugixml](https://github.com/zeux/pugixml) | 1.10 snapshot | MIT |
| `.../3rdparty/taskflow` | [Taskflow](https://github.com/taskflow/taskflow) | 3.1.0 | MIT |
| `.../3rdparty/mcf_solver` | [OpenPARF router source](https://github.com/PKU-IDEA/OpenPARF/tree/793cac2bb109e5cc76046f87d39ed70fa093cf60/openparf/routing/fpga-router/3rdparty/mcf_solver) | OpenPARF pinned snapshot | Source notices |

## External open-source build and runtime dependencies

These projects are not copied into EmuFlow. They are normal compiler,
build-system, library, or Python runtime dependencies and never substitute for
an EmuFlow optimization engine.

| Dependency | Upstream source | Required by |
| --- | --- | --- |
| CMake | [Kitware/CMake](https://github.com/Kitware/CMake) | Root and engine builds |
| GNU Make | [GNU Make](https://git.savannah.gnu.org/cgit/make.git) | Root, Yosys, and CUDD builds |
| GCC or LLVM/Clang | [gcc-mirror/gcc](https://github.com/gcc-mirror/gcc), [llvm/llvm-project](https://github.com/llvm/llvm-project) | C/C++17 compilation |
| Python | [python/cpython](https://github.com/python/cpython) | Control plane, checkers, OpenPARF |
| Boost | [boostorg/boost](https://github.com/boostorg/boost) | RePart, OpenROAD, OpenPARF |
| Bison | [GNU Bison](https://git.savannah.gnu.org/cgit/bison.git) | Yosys, OpenROAD, OpenPARF parsers |
| Flex | [westes/flex](https://github.com/westes/flex) | Yosys, OpenROAD, OpenPARF parsers |
| Tcl | [tcltk/tcl](https://github.com/tcltk/tcl) | OpenROAD command interface and VPR SDC parsing |
| SWIG | [swig/swig](https://github.com/swig/swig) | OpenROAD/OpenSTA bindings and VPR SDC parser generation |
| Eigen | [libeigen/eigen](https://gitlab.com/libeigen/eigen) | OpenROAD/OpenSTA |
| zlib | [madler/zlib](https://github.com/madler/zlib) | Yosys/OpenROAD |
| spdlog | [gabime/spdlog](https://github.com/gabime/spdlog) | OpenROAD logging |
| LEMON | [OpenROAD lemon-graph](https://github.com/The-OpenROAD-Project/lemon-graph) | OpenROAD graph algorithms |
| OR-Tools | [google/or-tools](https://github.com/google/or-tools) | TritonPart and OpenROAD optimization |
| OpenMP runtime | [LLVM OpenMP](https://github.com/llvm/llvm-project/tree/main/openmp) or compiler equivalent | OpenROAD and OpenPARF parallel kernels |
| PyTorch | [pytorch/pytorch](https://github.com/pytorch/pytorch) | OpenPARF tensor operators |
| NumPy | [numpy/numpy](https://github.com/numpy/numpy) | OpenPARF arrays |
| PyYAML | [yaml/pyyaml](https://github.com/yaml/pyyaml) | OpenPARF configuration |
| Hummingbird | [microsoft/hummingbird](https://github.com/microsoft/hummingbird) | OpenPARF learned delay/congestion models |
| NetworkX | [networkx/networkx](https://github.com/networkx/networkx) | OpenPARF utilities |
| tqdm | [tqdm/tqdm](https://github.com/tqdm/tqdm) | OpenPARF progress reporting |
| RapidWright | [Xilinx/RapidWright](https://github.com/Xilinx/RapidWright/tree/127f55cd704c277372697e699f1559e1cdc91f34) | Optional Route A no-route DeviceResources generation, native RWRoute graph, physical metadata, and RWRoute; source is Apache-2.0, while packaged Xilinx device data remains Xilinx-EULA governed and external |

Qt, Doxygen, GoogleTest, CPLEX, VTune, and CUDA are optional upstream build
features. The default EmuFlow build does not require Qt, Doxygen, CPLEX,
VTune, GUROBI, or CUDA. Vivado and GUROBI are proprietary optional tools and
are deliberately outside the open-source inventory.

RapidWright is also outside the in-tree source closure. The pinned provider
contract is public and reviewable, but generated device data is never
redistributed or counted as an open EmuFlow engine. EmuFlow's Apache-2.0
streaming certificate generator validates the complete native route graph
without copying it into the repository or ArchitectureDB.

## Fetchable architecture sources

The default academic device model is not an opaque binary or commercial
database. `resources/architectures/vtr/flagship-k6-n10-40nm.json` pins VTR's
[flagship heterogeneous architecture XML](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/a3e60c31bb4384373d2d1a43c38c7569723733b6/vtr_flow/arch/timing/k6_frac_N10_frac_chain_mem32K_40nm.xml)
at commit `a3e60c31bb4384373d2d1a43c38c7569723733b6`, records its MIT
license and SHA-256, and is verified before use. The C++ importer and its XML
regression fixture are first-party source in this repository.

## RTL benchmark sources

Benchmark source is not committed into the monorepo. The fetcher uses pinned
revisions from [`benchmarks/rtl_catalog.json`](benchmarks/rtl_catalog.json) and
places downloaded data in the ignored `third_party/rtl/` cache.

| Benchmark | Upstream source |
| --- | --- |
| SERV | [olofk/serv](https://github.com/olofk/serv) |
| PicoRV32 | [YosysHQ/picorv32](https://github.com/YosysHQ/picorv32) |
| secworks AES | [secworks/aes](https://github.com/secworks/aes) |
| Ibex | [lowRISC/ibex](https://github.com/lowRISC/ibex) |
| VTR classic RTL | [verilog-to-routing/vtr-verilog-to-routing](https://github.com/verilog-to-routing/vtr-verilog-to-routing) |
| Koios 2.0 | [VTR Koios benchmarks](https://github.com/verilog-to-routing/vtr-verilog-to-routing/tree/master/vtr_flow/benchmarks/verilog/koios) |
| VeeR EH1 | [chipsalliance/Cores-VeeR-EH1](https://github.com/chipsalliance/Cores-VeeR-EH1) |
| NVDLA | [nvdla/hw](https://github.com/nvdla/hw) |

## Public multi-FPGA benchmark inputs

Benchmark inputs are fetched into ignored working directories and are not
vendored into the source tree. The 2025 EDA Elite cases 01--04 come from the
MIT-licensed
[nsyw705/EDA-2025-git repository](https://github.com/nsyw705/EDA-2025-git/tree/45315b739e6678bf04605aaa246285c768bc8e13/data_case)
at commit `45315b739e6678bf04605aaa246285c768bc8e13`.
`scripts/fetch_eda2025_benchmarks.py` verifies every input by SHA-256 and records
its provenance. Only the four input text files per case are fetched; neither
participant algorithm source nor the opaque checker executable is incorporated
as an EmuFlow component.

## CI actions

The source-complete workflow uses
[actions/checkout](https://github.com/actions/checkout) v4 and
[actions/setup-python](https://github.com/actions/setup-python) v5. They run
only in GitHub Actions and are not shipped as EmuFlow implementation source.
