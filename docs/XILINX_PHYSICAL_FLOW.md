# Xilinx physical flow

This document describes the public contract of EmuFlow's Xilinx physical
backend, including the implementation stages and their acceptance criteria.

## Backend contract

The physical backends publish the same high-level result contract while using
different device models and implementation engines:

| Backend | Device model and implementation |
| --- | --- |
| RapidWright | Supported Xilinx device resources and site/BEL connectivity, native OpenPARF placement, validated site/BEL binding, and RWRoute detailed routing |
| VTR | VTR architecture XML, VPR packing and routing-resource graph, and either OpenPARF or VPR placement |
| Vivado | Optional vendor synthesis and implementation integration for supported inputs |

RapidWright device data describes a device, not the multi-FPGA board around it.
Board topology, fixed package-pin connections, link capacity, and inter-FPGA
latency come from the separate [hardware platform model](HARDWARE_PLATFORM.md).

## Placement preparation

Phase 7 maps the split netlist and generated transport logic to the selected
device model. Ordinary high-fanout nets in the transport design are converted
to deterministic balanced LUT1 buffer trees before native OpenPARF placement.
Clock and reset networks keep their dedicated handling. The added LUTs remain
subject to the same per-FPGA resource limits as all other cells.

The mapped SQLite exporter retains primary-key and pin constraints during
insertion, then bulk-builds the auxiliary pin indexes before atomic publication.
No consumer can observe a partially indexed netlist. This avoids per-pin random
updates to the net-order index during large physical input generation.

For mapped index v2, `emuflow_xilinx_bookshelf_exporter` reads the typed cell,
pin, and validated atom relations directly. Typed export resolves this required
executable before reconstructing macro facts, atoms, or device geometry. An
external native install is selected with `EMUFLOW_NATIVE_ROOT`; a missing tool
fails before full-design preparation. C++ checks atom ownership and types,
contiguous port bits, pin directions, and multiple drivers, then streams nets in
numeric net order with drivers first. Constants remain in primitive pin
signatures but do not become net endpoints; single-endpoint nets are omitted.
Only primitive pin signatures and aggregate counts return to Python, which
renders the small library and atomically publishes the net file. The exporter
opens its frozen input relations with explicit immutable read-only URIs and
uses the SQLite 3.22+ development library at build time. Materialized JSON callers
retain the reference exporter for explicit compatibility and differential tests.
This removes one graph-sized Python traversal; mapped lowering, physical macro
preparation, and timing-query preparation are separate stages.

Atomic-source construction, Bookshelf export, and placement validation reuse
the physical macro derivation owned by the same live immutable mapped reader.
Consumers receive detached records, so modifying one returned contract cannot
change later validation. Closing the reader releases this derived state. A new
reader, a different mapped file/top, or a mutable JSON input derives its own
facts; no persistent analysis artifact or cross-run cache is introduced.

The physical bridge reuses a site's loaded mapped-cell pin records for its
capacity and FF control-set checks. This working set is bounded by one physical
cluster and is discarded before the next site. Certificate validation and the
independent handoff validator still load and check their own source records;
driver uniqueness and dedicated topology checks retain their full scope.

Native and continuous OpenPARF drivers load the calling EmuFlow package through
an explicit package entry point under the selected OpenPARF interpreter. They
do not require EmuFlow to be separately installed in that interpreter or add
the coordinator's site-packages directory to its dependency search path. The
child retains its own OpenPARF/PyTorch environment and compiled ABI.

The native SLL multiplier keeps OpenPARF's `[0,1000)` acceptance policy and
uses the fixed `t_psi=0.002` Adam step from LEAPS Equation (23). This is the
incremental component of the former `1.002` exponential growth rate, rather
than that rate itself. The current SLL-count delta receives the paper's
`rho=0.9` EMA weight before entering the Adam moments. Applying the absolute
delta again as an exponential factor makes the update depend exponentially on
design size. Oversize positive updates are rejected and negative updates clamp
at zero without disabling SLL optimization or relaxing placement checks.

OpenPARF applies a bounded square-root weight to large nets in its analytical
placement objective. This guides placement without changing connectivity.
The preferred logic-filler row limit may be exceeded when necessary to keep
each sample small enough for the density grid. The filler budget is
`max(target_density * placeable_area - movable_area, 0)`, where placeable
area excludes unavailable and fixed area. The electrostatic operator already
scales unavailable/fixed charge by the same target. For a feasible target,
this makes total charge equal to target density times die area. Filling to
full physical capacity would optimize a different density than the overflow
metric. Native startup independently checks both the budget and sampled
area; new convergence certificates record the budget and its provider.
Historical certificates remain readable with their recorded policy.
ISM detailed placement uses the same configured routing and pin capacities
as the analytical congestion estimators. Capacities must be finite and
positive; zero defaults would produce invalid utilization and remove usable
whitespace from matching. Each CLB/BLE/LUT-FF stage clears demand maps before
accumulating current demand, then normalizes once. Pin demand uses its own
bin area. These estimates guide whitespace selection; they do not certify
physical routing capacity or replace exact RWRoute validation.
Fixed atoms also reserve their occupied matching sites at CLB, BLE, or
LUT/FF-role granularity. Co-occupants stay fixed for that stage, and reserved
sites cannot receive whitespace fillers. This is required for fixed carry
and LUT/MUX macros: keeping their coordinates unchanged alone does not stop
another cluster from moving onto the same BELs. Native checks run before and
after each stage to reject duplicate logic slots or moved fixed atoms. Runtime
qualification must include a fixed LUT macro, alongside the ordinary logic
fixture; an ordinary movable-only fixture cannot establish this property.
An operator-only qualification build is available through
`scripts/validation/build_openparf_ism.py`. The checked overlay installer
accepts that exact-commit build receipt using `--native-ism-manifest`, checks
the binary hash and Python ABI suffix, and retains the unchanged base runtime.
The sampling constraint keeps
each LUT/FF filler within half a density bin. Native startup validates the
actual filler count, size, bin size, and span, and the convergence certificate
records those values. Preserving total filler area alone is insufficient:
coarse samples alias the density field and can leave overflow flat while the
placement multipliers diverge. For square logic fillers, the half-bin bound
requires about four times the sampling rows of the former full-bin bound.
Exact legalization and an independent site/BEL validator remain responsible
for physical legality.

Global-placement overflow is an analytical quality signal, not a site/BEL
legality rule. The normal pass target is `0.10`. The representative diagnostic
path reaches the `0.65` RUDY/pin-area trigger, then records eight metrics after
the inflation event and restores the candidate with the smallest inflated LUT
or FF overflow, breaking ties by HPWL. The fixed window prevents global
placement from withholding DirectLG and RWRoute while it repeatedly optimizes
an inflated proxy. Its certificate records the adjustment iteration, sampled
window, mean and maximum legalization displacement, post-legalization HPWL,
and exact legality. A diagnostic handoff does not authorize routing by itself;
the exact validator and negotiated router remain authoritative.

The native atomic route chooses one density target from the largest exported
resource utilization plus 2.5 percentage points of analytical headroom, bounded
to `0.40..0.75`. The target is applied to the complete qualified site domain;
the exporter does not pre-reserve a second fraction of sites by clock region or
site type. This avoids multiplying two whitespace factors and lets the density
optimizer decide its spatial distribution. The target limits the analytical
density field; it does not guarantee that a sparse partition will occupy every
SLR. The NVDLA fpga1 candidate at `0.40`, for example, legally selected the two
central SLRs. Exact legalization, detailed placement, and RWRoute remain the
acceptance tests, and their HPWL and congestion results decide whether the
placement is usable.

The current open backend has no placement-aware physical-synthesis pass between
the preliminary placement and RWRoute. See
[the Phase 7 physical-optimization gap and implementation plan](PHASE7_PHYSICAL_OPTIMIZATION.md)
for the distinction between congestion-aware placement and structural physical
optimization.

Area adjustment changes the analytical cell areas and resets the nonlinear
optimizer. Its new overflow values therefore are not comparable to the
pre-adjustment `0.65` trigger as a physical legality test. Waiting for the
inflated metric to approach the normal target caused long global-placement loops
and could favor wirelength or density proxies that did not improve routing.
Any later inflation resets the saved window. The handoff always contains a
metric newer than the last inflation event, so congestion feedback is present
without treating its perturbed overflow as a downstream gate.

The conservative UltraScale+ model qualifies eight independent 6LUT BELs and
sixteen FF BELs per slice. DirectLG retains sixteen LUT z positions to encode
the 5LUT/6LUT pairs, with paired LUT use disabled. That encoding is not physical
density capacity: each analytical LUT consumes `1/8` of a slice, each FF `1/16`.
The density headroom check and filler count use the same qualified capacity.
The `resource_unit_capacity` manifest field records these density units; the
Bookshelf site grammar retains sixteen LUT slots for native BEL numbering.
This leaves the configured target density and final site/BEL checks intact.

Bookshelf's column grammar stretches each site to the next entry. The exporter
therefore inserts zero-resource entries after a logic site when physical gaps
follow it. LUT/FF sites occupy one native grid row; hard-block
spans keep their qualified heights. These unavailable entries are not physical
sites and never appear in the atom-to-site database. The driver compares the
already constructed native density tensors with the exported LUT/FF site-area
totals before optimization. Missing or mismatched area contracts fail closed;
regenerate native inputs when continuing from an older configuration.

The OpenPARF coordinate importer joins solver rows to the authenticated atomic
name-map sidecar through an explicitly URI-enabled SQLite connection. The
sidecar is attached read-only and immutable; the join independently checks atom
identity and exact coverage without expanding the complete name map in Python.

The default placement region covers all SLRs of the device. The platform's
per-FPGA utilization limit is a whole-device capacity contract and does not
implicitly create a pblock. Using the whole device lets the placer trade
wirelength for lower local density and better routability.

Clock-region/site-type buckets likewise expose their complete nonoverlapping
site capacity. They are reported as diagnostics, not capped below the physical
capacity. Exact site/BEL validation establishes placement legality and RWRoute
establishes whether the resulting placement is routable.

## Detailed routing and validation

The validated placement is passed to RWRoute. Routing is complete only when
all required connections are routed, no capacity overlap remains between
different nets, and the independent route certificate can resolve every
covered logical net to its physical nodes and PIPs. Iteration diagnostics
include congested connections, overused-node counts, excess users, maximum
occupancy, and SLR distribution. Mutually exclusive fanout buckets count each
overused node once according to the largest fanout among its users. This
distinguishes congestion involving high-fanout nets from congestion contested
entirely by small nets; top-net rankings alone cannot establish the dominant
population. Bucket counts must sum to the complete overused-node count.

Clock-region capacity, clock-tree legality, ordinary-net legality, and route
coverage are separate checks. Passing a coarse clock-region capacity audit is
necessary when that audit applies, but it does not by itself certify complete
clock routing or clock timing.

## Fixed differential I/O

Phase 7 consumes the sealed Phase 6 binding, differential adapter, and XDC
files. Each used virtual-board lane must resolve to the same top-level port,
P/N package pins, bank, direction, and schedule entry recorded by Phase 6.
The RapidWright adapter materializes the corresponding differential I/O
boundary and includes it in route coverage and boundary timing.

A mismatch between the wiring inventory, binding, generated RTL, or XDC is a
hard failure. The resulting route validates the declared research platform;
electrical board signoff still requires the real board's power, clock, reset,
connector, termination, and signal-integrity constraints.

## Timing publication

Per-FPGA physical timing records logic and interface delays from the routed
implementation. EmuFlow then binds those delays to the selected TDM events and
BoardDB link-delay model for global analysis. OpenSTA is the default global STA
engine. Its WNS/TNS values are meaningful only for the declared TimingPathDB
population and the sealed physical/platform inputs described in the
[global timing qualification](global-opensta-qualification.md).

Passing partition resource checks, placement legality, or routing alone does
not establish whole-design timing closure. Phase 7 publishes a successful
whole-design result only after every selected FPGA record and the global timing
contract pass their independent validators.
