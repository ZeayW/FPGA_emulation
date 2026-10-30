# Source layout

The implementation is organized by responsibility. Source directories do not
select algorithms or change artifact schemas.

| Location | Responsibility |
| --- | --- |
| `src/core` | Shared C++ implementation library |
| `src/cli` | C++ executable entry points |
| `src/python/emuflow` | Python package and remaining tool/control adapters |
| `tools/emuflow_tools` | Separately installed research and dataset tools |
| `tests` | Independent validation, bounded fixtures and regression tests |
| `engines` | Third-party tool sources |

Both core and Python components use domain directories: frontend, partition,
routing, scheduling, transport, physical and timing. Flow orchestration lives
under `flow`; common process/error facilities, storage mechanisms and IR models
have their own directories. Stage numbers describe execution order rather than
defining a second implementation hierarchy.

Physical code shared by both maintained backend chains lives in `physical`.
Tool-specific adapters live under `physical/openparf`, `physical/rapidwright`
and `physical/vivado`. Chimew lives under `transport/chimew`.

`src/core/sources.txt` is the implementation manifest used by CMake, native
test builds and runtime deployment. Header includes name their owning domain;
generated SQL schema headers live under `storage/schema` in the build tree.
Python resources remain in `emuflow/data`, with their location resolved through
`emuflow.common.paths` independently of a consumer module's depth.

Python consumers import the current domain module directly, for example
`emuflow_tools.stage_clients.partition` or `emuflow.physical.vivado.vivado_physical`.
Retired flat module paths have no forwarding packages or aliases.

The installed commands remain `emuflow` and `emuflow-native`. Directory
organization does not imply that the remaining Python flow has migrated to C++.
