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

For supported Xilinx device inputs, the RapidWright route uses native
OpenPARF placement, independently validates the site/BEL result, and passes it
to RWRoute. Device adapters do not replace native placement by a hidden
greedy site search. Alternative placer adapters are research interfaces, not
evidence that an unsupported native optimizer can implement the design.

Vivado is an optional proprietary physical backend. It is neither bundled nor
required by the default VTR route.

## Source and attribution

Upstream revisions, incorporated sources, licenses, and algorithm references
are recorded in [the component inventory](../OPEN_SOURCE_COMPONENTS.md) and
[the source manifest](../SOURCE_MANIFEST.json). Preserve those records when
changing an integration. A paper-informed extension must not be labelled a
faithful reproduction without equation-level and implementation validation.
