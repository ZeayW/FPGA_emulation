# Algorithms and providers

EmuFlow combines upstream EDA engines with project-specific system algorithms.
A provider name identifies an implementation and its contract, not a universal
quality-of-result guarantee.

## Frontend and timing

Yosys and ABC supply RTL synthesis and mapping. Mapping profiles preserve the
primitive namespace needed by the selected physical architecture.

OpenSTA supplies pre-partition timing paths and the default global physical
timing analysis. An optional Vivado timing adapter emits the common path
contract for supported device inputs. [Global timing scope](global-opensta-qualification.md)
distinguishes path-population coverage, event readiness, and hardware signoff.

## Partitioning

The full-flow CLI defaults to PATRON, using a TritonPart initializer on the same
cluster graph. PATRON refines capacity- and topology-legal assignments with
timing and communication-pressure objectives. Its predictions do not establish
physical timing closure.

Explicit alternatives include:

- TritonPart: upstream multilevel, multi-constraint hypergraph partitioning;
- RePart and replication-enabled RePart: separate provider choices whose
  replicated area and connectivity remain part of validation;
- MFSPart: a separate multilevel provider with coarsening, initialization,
  uncoarsening/refinement, and independent checks;
- greedy: an explicit baseline for controlled comparisons.

Boundary semantics are independent of provider selection. Sequential-only
and generalized [Static Exact](STATIC_EXACT_COMBINATIONAL_CUT.md) must not be
compared as if they admitted identical partition search spaces.

## System routing and TDM assignment

The native system router constructs multicast communication routes under
BoardDB reachability, direction, shared capacity, and hop limits. Timing-driven
selection uses path delay, congestion, and TDM-pressure costs.

TDM assignment allocates ratios, lanes, and slots on those routes. Ratio
optimization and slot refinement are distinct from system routing. Supported
combinational-cut modes additionally bind dependency readiness to concrete
events. Pre-physical optimization costs are predictions; the physical global
timing result is authoritative.

## Pin planning and transport

The baseline Phase 6 provider plans logical lanes/pins and generates per-FPGA
netlists and static transport RTL. Chimew is an explicit physical-input-aware
provider, not a substitute for missing placement, routing-capacity, or package
information.

Grouping/matching and bank/package assignment have separate constraints.
Transport, shadow registers, and frame-barrier synchronization preserve the
declared virtual-cycle semantics. Phase 6 intermediate objectives alone cannot
establish a timing improvement.

## Placement and routing

VPR supplies the open academic packing/routing model; OpenPARF supplies
analytical placement, legalization, and detailed placement.

The default research route is RapidWright. For supported Xilinx device inputs,
it uses native
OpenPARF placement, independently validates the site/BEL result, and passes it
to RWRoute. Device adapters do not replace native placement by a hidden
greedy site search. Alternative placer adapters are research interfaces, not
evidence that an unsupported native optimizer can implement the design.

VTR is an alternative academic backend. Vivado is an optional proprietary
physical backend and is not bundled. CLI and Python API defaults select
RapidWright with Xilinx mapping; see [backend selection](../README.md#configuration-and-limits).

## Source and attribution

Upstream revisions, incorporated sources, licenses, and algorithm references
are recorded in [the component inventory](../OPEN_SOURCE_COMPONENTS.md) and
[the source manifest](../SOURCE_MANIFEST.json). Preserve those records when
changing an integration. A paper-informed extension must not be labelled a
faithful reproduction without equation-level and implementation validation.

## Implementation and reference map

The [README research references](../README.md#selected-research-references)
provide publication links. This map identifies where those methods enter the
implementation so contributors can distinguish an upstream engine from an
EmuFlow algorithm or adapter.

| Component | Relationship to upstream work | Implementation entry point |
| --- | --- | --- |
| Yosys / ABC synthesis | Direct upstream tool execution; EmuFlow mapping profiles and import | [yosys.py](../src/emuflow/yosys.py) |
| OpenSTA path extraction | Direct STA-engine integration; EmuFlow path contract | [opensta.py](../src/emuflow/opensta.py) |
| TritonPart / RePart | Upstream partitioner integrations; shared assignment/legality contracts | [partition.py](../src/emuflow/partition.py) |
| PATRON | EmuFlow path-aware K-way refinement initialized by TritonPart; not an external paper implementation | [patron_refiner.cpp](../src/native/patron_refiner.cpp) |
| System router | EmuFlow implementation drawing on DAC 2025 delay-demand balancing and ASP-DAC 2026 timing-aware routing, with additional candidate trees | [tlr_router.cpp](../src/native/tlr_router.cpp) |
| TDM ratios | Published Lagrangian/KKT methods, discrete legalization, and EmuFlow platform constraints | [tdm_ratio_optimizer.cpp](../src/native/tdm_ratio_optimizer.cpp) |
| Timing-DAG ratio solver | ASP-DAC 2026 equation-based continuous optimizer; distinct from runtime dependency/event binding | [tdm_timing_dag_optimizer.cpp](../src/native/tdm_timing_dag_optimizer.cpp) |
| Concrete slot refinement | EmuFlow event-based local optimization after ratio assignment | [tdm_slot_optimizer.cpp](../src/native/tdm_slot_optimizer.cpp) |
| Placement-aware pin baseline | EmuFlow region grouping, proximity costs, and minimum-cost assignment | [placement_aware_pin_planner.cpp](../src/native/placement_aware_pin_planner.cpp) |
| Chimew initial grouping | Paper Algorithm 1 crossing-encoding grouping | [chimew_signal_grouper.cpp](../src/native/chimew_signal_grouper.cpp) |
| Chimew position refinement | Paper Section 3.3.2 with explicitly documented implementation choices | [chimew_refinement.py](../src/emuflow/chimew_refinement.py) |
| Chimew bank/channel assignment | Paper Section 3.4 two-stage min-cost-flow kernel | [chimew_bank_channel_assigner.cpp](../src/native/chimew_bank_channel_assigner.cpp) |
| Chimew integration | EmuFlow electrical and slot binding around the paper-derived output | [chimew_phase6.py](../src/emuflow/chimew_phase6.py) |
| Split netlists and transport | EmuFlow runtime implementation and validation | [phase6.py](../src/emuflow/phase6.py) |
| VPR / OpenPARF / Vivado | Physical-tool integrations selected by backend and device contract | [physical_backend.py](../src/emuflow/physical_backend.py) |
| RapidWright / RWRoute | Device-resource, site/BEL, and native routing integration | [rapidwright_provider.py](../src/emuflow/rapidwright_provider.py) |
| Global OpenSTA | EmuFlow timing-model exporter and upstream STA execution | [global_sta.py](../src/emuflow/global_sta.py) |

In particular, neither the Chimew paper's reported timing gains nor a placement
engine's published benchmark results transfer automatically to EmuFlow. Those
claims require complete physical runs under the same declared workload,
platform, constraints, and metric definitions.
