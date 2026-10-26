# Xilinx physical flow

This document describes the public contract of EmuFlow's Xilinx physical
backend, including the implementation stages and their acceptance criteria.

## Backend contract

The physical backends publish the same high-level result contract while using
different device models and implementation engines:

| Backend | Device model and implementation |
| --- | --- |
| RapidWright | Supported Xilinx device resources and site/BEL connectivity, native OpenPARF placement, validated site/BEL binding, and RWRoute detailed routing |
| Vivado | Optional vendor synthesis and implementation integration for supported inputs |

The maintained Xilinx implementation routes are OpenPARF/RWRoute and Vivado.
The unused AMF and DREAMPlaceFPGA feasibility adapters, candidate runners and
AMF certificate policy and unused multi-candidate comparator are removed.
Retired placer certificates are not
accepted by Xilinx placement validation. Common packing, capability reporting,
site/BEL ownership, cascade and typed physical-handoff validation remain part
of the retained routes. VTR/open execution, its standalone architecture/packing/
routing/timing adapters and engine build/install dependencies are retired.
Chimew consumes only the maintained Xilinx physical owners.

The RapidWright backend has one placement chain: native OpenPARF atomic
placement, independent site/BEL and macro checks, typed physical handoff, then
RWRoute. There is no `--rapidwright-placer` or
`--physical-rapidwright-placer` selector. The old packed-cluster guidance and
greedy legalizer flow, its JSON pack/place producers, native packed-cluster
smoke runner, and single-SLR planner are retired. Their `arch pack-xilinx`,
`place-xilinx`, `guide-xilinx-openparf`, `plan-xilinx-single-slr` and
`validate-xilinx-single-slr-plan` commands are removed. Independent packing,
site/BEL/cascade validators remain available. Small bounded packing and site
oracles live only in tests and are not installed; they do not launch a flow.
The obsolete JSON schema for the original exact-legalizer provider is removed;
current handoff schemas and semantic validators retain their existing contracts.

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

For mapped index v2, `emuflow-native phase7 export-bookshelf` reads the typed
cell, pin, and validated atom relations directly through `libemuflow_core`.
C++ checks atom ownership and types,
contiguous port bits, pin directions, and multiple drivers, then streams nets in
numeric net order with drivers first. Constants remain in primitive pin
signatures but do not become net endpoints; single-endpoint nets are omitted.
Only primitive pin signatures and aggregate counts return to Python, which
renders the small library and atomically publishes the net file. The exporter
uses two ordered cursors over one endpoint index instead of retaining one net's
fanout. It binds input generations, rejects aliased destinations, stages text
through the shared atomic writer, publishes the summary last, and retires its
owned scratch relation on success or failure. It opens its frozen input
relations with explicit immutable read-only URIs and
uses the SQLite 3.22+ development library at build time. Materialized JSON callers
retain the reference exporter for explicit compatibility and differential tests.
This removes one graph-sized Python traversal; mapped lowering, physical macro
preparation, and timing-query preparation are separate stages.

The separate CARRY8/LUT6_2-only exporter, runtime adapter, certificate and JSON
bridge are retired. Mixed-resource atomic placement owns carry macro and directed
chain validation. Shared native carry kernels and current legality tests remain.

## Typed placement handoff

`materialize_xilinx_openparf_atomic_contract(mapped, architecture, certificate,
physical_handoff, ...)` has one required SQLite output. The former pair of JSON
output arguments and optional typed-mode selector are retired. Production requires
the typed placement certificate and streams each validated site group directly to
the handoff builder; no complete physical-cluster tree is returned by the validator.
Packing and placement remain lazy views for independent circuit/site/BEL checks.

The publisher binds the declared source files and certificate/atomic-source
sidecars before consuming their records. It rejects aliased outputs, including hard
links, and checks bound generations immediately before atomic replacement. A source
change or failed independent validation preserves the previous published handoff.
Bridge source checks and header assembly reuse the same operation-local digests,
while publication checks bound generations. Independent native-device validation
retains its own source checks.
Small JSON validation fixtures are snapshots of this same typed producer under
`tests/openparf_bridge_fixture.py`; this bounded helper is not installed and does
not implement a second bridge algorithm.

## Native RWRoute export

The production API is `export_rwroute_input(mapped_index, physical_handoff, output)`.
Its CLI is `emuflow arch export-xilinx-rwroute-input --mapped-index MAPPED.sqlite
--physical-handoff PHYSICAL.sqlite --output INPUT.tsv`. The former separate
`--packed`/`--placement` JSON export inputs and Python-object API are retired.
The v2 bounded native request carries source identities, I/O and resource limits;
top and part come directly from the authoritative stores. It never opens Python
packing/placement views or copies their graph populations. Standalone export hashes
the physical handoff once; managed callers reuse the sealed source digests.
The independent Python TSV oracle lives in `tests/rwroute_reference.py` for explicit
qualification and is not installed or selected by production failures.

For mapped index v2 and a single typed physical handoff, the production bridge
requires `emuflow-native phase7 export-rwroute MAPPED PHYSICAL REQUEST TSV SUMMARY`.
The shared-core operation reads both immutable stores directly. The checked
request contains their file generations, source digests and resource
limits and fixed differential I/O configuration; it contains no placed-cell,
safe-name, gate or net population. A selected native failure stops the export.
The noninstalled test oracle remains available for explicit differential checks;
legacy JSON inputs are limited to qualification fixtures.

C++ emits the complete existing RapidWright TSV contract: resource/source META,
CELL, selected RAM PARAM, IOB, CLOCK_GATE, NET/PIN and EXCLUDED rows. Lexical cell
order, numeric net order and driver-first pin order are unchanged. LUT6_2 still
occupies the same two physical LUTs; unsupported routable pins reject. RAM binary
strings and arbitrarily large integer parameters retain their exact decimal
meaning. Large INIT payloads are not exported. DSP physical-count expansion and
CARRY8 shared-exit conflict checks retain their existing meanings.

The `native-rwroute-export-v2` implementation builds a source-scoped cell catalog
during CELL emission: route ordinal, interned site ID and primitive/gate flags
occupy 16 bytes per source cell. Sparse nonnegative mapped sequences use an
additional ordered integer ID column; identities never cross artifact boundaries.
The temporary site-name interner is released before clock and net processing.
Clock qualification reduces distinct enable nets once, then distinct root/output
nets once. A reverse-dependency queue computes the existing closed-gate-chain
policy, including cycles; private SQLite scratch contains selected net IDs rather
than endpoint copies. There is no full physical-cell window computation or
candidate-by-candidate fanout scan.

The pin cursor checks source sequence/name agreement and consumes the integer
catalog directly. Only one live net's endpoints retain port names; cell/site/type
strings are not copied per pin. Emitted nets restore canonical cell/port/bit and
driver-first order. CARRY8 exit conflicts use per-cell flags rather than a second
SQL relation. The summary records catalog bytes, visited pin rows, sparse clock
work and maximum live net endpoints. These are implementation metrics; the
logical TSV, clock policy and summary envelope remain unchanged. No gate or
safe-name map returns to Python. Top-port direction/width/bit bindings, duplicate physical I/O bits,
source cell/type ownership, schema versions, request CRC and source generations
are checked before publication. Physical handoff readers use the common escaped
immutable URI, including paths containing `?`, `#`, `%` or Unicode.

The common stage API owns artifact binding, structured errors/metrics and
aliased-destination rejection. TSV publication uses the common atomic writer;
the bounded `emuflow.rwroute-export-summary/v1` is published last. Java/RWRoute
still owns actual routing,
and the independent site/BEL/connectivity, resource, DRC and timing gates remain
required. Native export parity alone does not qualify physical implementation
or complete the native data-plane migration.

Atomic-source construction, Bookshelf export, and placement validation reuse
the physical macro derivation owned by the same live immutable mapped reader.
Consumers receive detached records, so modifying one returned contract cannot
change later validation. Closing the reader releases this derived state. A new
reader, a different mapped file/top, or a mutable JSON input derives its own
facts; no persistent analysis artifact or cross-run cache is introduced.

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
each LUT/FF filler within half a density bin. Native startup validates the
actual filler count, size, bin size, and span, and the convergence certificate
records those values. Preserving total filler area alone is insufficient:
coarse samples alias the density field and can leave overflow flat while the
placement multipliers diverge. For square logic fillers, the half-bin bound
requires about four times the sampling rows of the former full-bin bound.
Exact legalization and an independent site/BEL validator remain responsible
for physical legality.

Global-placement overflow is an analytical quality signal, not a site/BEL
legality rule. The normal pass target is `0.10`. When the optimizer reaches a
stable candidate between `0.10` and `0.25`, the representative diagnostic path
restores the lowest-HPWL bounded checkpoint and runs DirectLG. Its certificate
records mean and maximum legalization displacement, post-legalization HPWL,
and exact legality. A diagnostic handoff does not authorize routing by itself;
the placement and initial congestion evidence must be reviewed first.
The diagnostic patience window is measured from the last improvement to the
best bounded HPWL. A smaller analytical overflow alone does not reset it,
because the density multiplier can otherwise keep reducing overflow while
wirelength and SLL cost diverge.

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
or reserved rows follow it. LUT/FF sites occupy one native grid row; hard-block
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

Clock-region/site-type buckets expose their complete nonoverlapping physical
capacity. Analytical density reserves whitespace without deleting a fraction of
qualified sites. Site/BEL uniqueness and whole-device resource limits remain hard
checks; local utilization is diagnostic within physical capacity. RWRoute must
still establish routability. The site-domain contract records
`full-qualified-site-domain`; the routability policy is
`openparf-rudy-pin-stable-feasible-full-sites-v8`.

## Detailed routing and validation

The validated placement is passed to RWRoute. Routing is complete only when
all required connections are routed, no capacity overlap remains between
different nets, and the independent route certificate can resolve every
covered logical net to its physical nodes and PIPs. Iteration diagnostics
include congested connections, overused-node counts, excess users, maximum
occupancy, and SLR distribution.

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

## Vivado indexed provider

The optional `vivado` backend consumes the current compact Phase 6 split and
transport contracts. It streams native Xilinx primitives through the canonical
mapped SQLite index into RTL, then runs Vivado synthesis, optimization,
placement, physical optimization, and routing in out-of-context (OOC) mode.
Fixed data pads are implemented, while external clock sources remain abstract;
absolute clock insertion/skew and board signoff are not qualified. This scope
is retained in each physical result and its summary projection. The common
physical-result schema accepts the native resource dimensions, resource-budget
evidence, and explicit OOC analysis scope. The whole-EmuIR execution adapter,
independent pre-placement Vivado TimingPathDB producer, and sampled-export
controls are removed. Current Vivado export and physical execution reject JSON
EmuIR inputs; there is one indexed implementation chain. Shared primitive
conversion remains for explicit Xilinx reference exports, and the board timing
consumer uses the current streaming log and indexed boundary owners.
Board-top generation reads only indexed design metadata and ports, releasing
the view before implementation; it does not load a circuit merely to connect
the already mapped partition.
CMake replaces its managed Vivado script directory so deleted scripts cannot
remain executable in an updated install. This integration is undergoing
real-device qualification; unit tests alone do not establish routed or global
STA success.

The standalone `sta emit-vivado-cut-map`, `import-vivado-tsv`,
`emit-vivado-net-map` and `import-vivado-path-database` commands, their vendor
path-provider policy and cut-path script are removed with the old timing
producer. Current OpenSTA net maps, path import/projection and original-path
validation remain; Phase 7 vendor boundary, local and logic measurements use
their dedicated indexed owners. Historical vendor TimingPathDB artifacts must
be examined with the matching Git version.

### Local implementation clock baseline

Indexed Vivado implementation uses explicit DUT targets independently of the
virtual runtime calendar. `--vivado-dut-frequency-mhz` on `multi-fpga physical`
and `--physical-vivado-dut-frequency-mhz` on `multi-fpga compile` set the
fastest DUT root implementation frequency. All original root periods scale by
one common factor, retaining their identities and frequency ratios on every
partition. For example, a 10 MHz override maps equal 10 ns source roots to
100 ns, while source roots of 10/20 ns become 100/200 ns. Omitting the override
uses the explicit source targets. Missing source periods remain errors; the
runtime frame is never a fallback. Source target metadata is preserved.

The implementation XDC and native admission manifest share one clock contract.
The transport word clock is named `emuflow_tdm_clk`; its legacy RTL port and
BoardDB field names remain `fabric_clk` and `fabric_clock_mhz`. Bundled research
platforms configure its period as 5 ns. A different DUT target does not scale
the TDM clock. The manifest and physical result distinguish source targets,
implementation targets and virtual runtime periods. Worker assembly rejects a
missing clock policy or a mixture of implementation targets. A synthesized
continuation must match the newly generated XDC, so an old frame-based clock
cannot silently survive through checkpoint reuse.

This first baseline does not feed exact per-cut budgets back into Vivado.
It supplies local implementation targets, extracts routed local/interface
measurements, and requires the global OpenSTA path analysis for system timing.
Local WNS alone cannot establish a safe multi-FPGA frequency. Final operating
period and frequency must use complete original-path coverage and the original
edge/phase relationships; they are calculated against the declared board/PHY
model, not measured board operation. Source-target and implementation-target
comparisons must remain separately labelled.

`scripts/vivado/inspect_selectio_ip.tcl PART EMPTY_OUTPUT_DIRECTORY` records
the installed High Speed SelectIO Wizard definition and its configurable defaults
for PHY integration. Its default mode creates one private IP customization without synthesis
or implementation; its observations do not qualify a serial PHY. An optional
third argument names a literal TSV with header `property<TAB>value` and unique
`CONFIG.*` properties, ordered so enabling controls precede their dependents.
Pin-enable fields (`CONFIG.ENABLE_BYTE<n>_PIN<n>`) require lowercase `true`
or `false`: the native wizard contains string-sensitive consumers of these
Boolean values, so an accepted noncanonical spelling can produce different
ports. Ignored or changed requested settings fail before generation. This explicitly
configures the private IP and generates
its output products and status report. The tool records resulting property
values. A fourth literal argument, `synthesize`, additionally synthesizes the
configured IP out of context and records its port census, I/O, clock and resource
reports. This does not perform placement, routing, link training or board timing
qualification.

A bounded package-derived recipe emits the literal configuration for the
bundled twenty-data-pair HP-bank arrangement:

```bash
python -m emuflow.lvds_selectio --bank 19 --direction tx --output build/tx-bank.tsv
vivado -mode batch -source scripts/vivado/inspect_selectio_ip.tcl \
  -tclargs xcvu19p-fsva3824-2-e build/tx-bank-inspection build/tx-bank.tsv synthesize
python -m emuflow.lvds_selectio --bank 19 --direction tx \
  --check-observations build/tx-bank-inspection
```

Use a fresh empty inspection directory and an existing output parent. The RX
recipe uses `--direction rx`. Both select 8-bit words at 1.6 Gb/s and reserve one
GC/QBC pair for the 800 MHz forwarded clock, separately from the twenty data
pairs (160 parallel bits per 5 ns word cycle). The TX clock word is `8'h55`;
this is an expected wrapper binding, not a claim that the recipe instantiates
the clock driver. Native byte/bit positions come from the official pin-function
fields; data pairs and clock pairs retain their distinct P/N identities. RX
uses the wizard's fixed `clk_p`/`clk_n` names and requests bitslip and the
multiple-interface handshake. Resulting ports must be checked against the
native products before connecting a physical top. The recipe does not supply
training, reset sequencing, FIFO capture or the physical boundary latency.
Configuration generation and IP synthesis remain intermediate PHY diagnostics.
Frequency enumeration strings retain the wizard's canonical decimal spelling;
numerical equality does not guarantee that a customization accepts an alias.
The bounded observation checker verifies the requested settings, selected P/N
package locations, physical port directions and every bit of each expected
parallel word. Native RX complement-word outputs are counted separately and
never double payload capacity. Its pass status qualifies this configuration
and port census only; a physical top must still bind every control input and
replace out-of-context clock placeholders with the actual clock networks.

`lvds_bank_phy.py` emits a complete native bank interface: twenty independent
8-bit payload words, a separate forwarded-clock word, calibration/PLL status,
receive FIFO application clocks and held alignment control. Every native input
must have a connection, and the observation checker compares the entire native
port population against the supported customization. RX complement words are
unused outputs. TX sends the native `0x2c` training pattern until the external
payload-enable consensus; the forwarded clock word remains `0x55`. The native
`start_bitslip` control is a held level: deasserting it resets alignment. The
caller must supply global startup consensus and the synchronous application
controls. These bank interfaces do not independently implement a distributed
startup barrier or prove a 30 ns fixed arc.

The explicit native behavioral loopback qualification exercises those interfaces
with unmodified generated IP and vendor models:

```bash
PYTHONPATH=src python3 scripts/validation/qualify_lvds_word_link.py \
  --vivado vivado --bank 19 --out build/native-word-link
```

It runs reset/calibration and held alignment before switching to 64 complete
160-bit payload words. Its scoreboard requires consecutive, uncorrupted words
and measures their TX-word to receiver-capture latency at application clock
edges. `declared_30ns_capture_window_feasible_in_this_simulation` distinguishes
digital word integrity from the separate six-cycle capture-window check. A
passing zero-cable behavioral simulation cannot qualify routed I/O timing,
cable/SI delay, board skew or the fitted fixed component. The fixed-pin word
BSP and full-system global timing gates remain independently required.
Simulator fatal/error outputs invalidate the qualification even if Tcl resumes
the simulation and a later success file appears. Startup timeout literals use
explicit 64-bit time arithmetic; a scalar output file cannot override a failed
simulation lifecycle.

The optional `--rx-alignment word-two-stage-v1` and `word-one-stage-v1`
candidates request native raw receive words (`ENABLE_BITSLIP=0`) and add an
EmuFlow byte aligner with two or one output register stages, respectively.
The default remains `native-bit-slip-v1`. Four consecutive training
matches select one of eight serial bit phases; the selected phase is held
through payload. Loss of admitted FIFO validity latches an error until reset
and retraining. This rotates bits across adjacent words; it does not establish
whole-word lane deskew or a physical delay. Independent serial-stream RTL tests
cover every phase and payload byte, fixed pipeline age, reset/retraining and
invalid-data rejection. The qualification also compares the complete generated
XCI boundary, including vector order and unused outputs, with the wrapper.

`fixed_30ns_word_arc_matches_in_this_simulation` requires both measured latency
bounds to equal six word cycles. A shorter variable or constant latency does
not by itself match a six-cycle table: it would require explicit normalization.
Neither this field nor the deadline-only field qualifies the fitted board arc.

The indexed provider's explicit native-word preparation consumes the v2 word
PHY plan and canonical wiring. It checks bank direction, peer, logical slices,
package pairs and forwarded-clock identity before emitting a top. Each active
bank instantiates the complete twenty-lane native IP and its wrapper. Unused
receive-bank slices are tied off; no original circuit graph is reconstructed.
The preparation's sources/configurations are sealed in its report. It selects
the plan's receive policy explicitly, generates the unmodified vendor IP during
top synthesis and does not impose KEEP/DONT_TOUCH on design cells.

The native word top exposes RIU, common TX readiness, global PLL lock,
held alignment and payload-enable service inputs, plus local readiness/error
outputs. RX reset remains asserted until the common TX-ready input. Global
startup/readiness consensus and pausible DUT-clock generation are external OOC
services. These ports do not implement a distributed barrier. The TDM and RIU
references feed explicit global buffers; the mapped transport and native bank
application interfaces share the buffered TDM reference. Primary periods are
explicit: TDM at 5 ns, RIU at 10 ns, and each RX forwarded clock at 1.25 ns.
All generated clocks must trace to these declared roots; unexpected primary
clocks, missing masters and cycles are rejected.
The routed native clock census is retained. Infrastructure-only setup paths
must meet their fixed clocks before this PHY model is admitted; a slow DUT
target cannot relax them. DUT-to-TX paths remain physical measurements for
schedule readiness, rather than being counted as infrastructure closure.

Word timing queries use native sampling boundaries. A TX query resolves its
serializer through the fixed P pad and differential output buffer, then selects
its parallel D bit using the source-bound eight-bit native recipe. Aliased
payload nets cannot select a different lane's capture pin. Synthesis may fold
duplicate native input ports and tie unused hierarchy pins low; those hierarchy
pin nets are therefore not used to identify TX sampling points. RX queries start at the registered alignment
output. These bounded physical resolutions are cached only within one export.
For a read-only connection diagnostic, `scripts/vivado/inspect_word_boundaries.tcl`
accepts `INPUT_DCP WORD_BOUNDARIES_TSV OUTPUT_TSV`. It inspects two boundaries
of each direction and the pad-anchored serializer data pins; this does not
qualify implementation or timing.
The `native-word-registered-interface` boundary scope distinguishes these
measurements from pad-to-pad timing. Its RX word-to-shadow path lies inside the
fixed transfer window; it must not be added to the fixed PHY arc again. The
following shadow-to-DUT cone still includes the shadow register's clock-to-Q.

Run the separate synthetic native-bank physical/query qualification with:

```bash
PYTHONPATH=src python3 scripts/validation/qualify_vivado_word_physical.py \
  --vivado vivado --out build/native-word-physical
```

This uses the common indexed implementation and source-proof machinery, fixed
P/N pins and a 10 MHz local DUT target. It is an integration fixture, not a
Phase 1--7 or global-design timing result. Serial-data pads do not receive the
one-bit adapter's zero-delay constraint relative to the TDM clock. Their fixed
transfer remains the external link model; routed serial-pad/board/SI signoff is
outside this qualification.

The multi-FPGA Phase 7B entry accepts the canonical word-mode Phase 6 bank plan
and wiring with `backend=vivado`. Before starting workers it checks source-bound
bank allocation and indexed transport lane occupancy. Workers use native banks
and fixed pins; they do not instantiate the one-bit differential adapter.
Every active FPGA must provide its matching word-plan/wiring identities,
fixed-clock infrastructure timing and fixed-pair evidence. Physical batches
and their common assembler carry these checks independently of Phase 7C.
The assembled result is a local physical result; complete-fanin word event
projection and global timing remain required before global WNS/TNS or frequency
can be reported. The global word-mode timing guard remains in place.

For original top-level DUT data I/O, the compile flow forwards its explicit
`timing_io` environment to Vivado. Standalone Python callers may pass the same
bounded object as `dut_timing_io` to `run_multi_fpga_physical_flow`. The physical
entry checks its identity against the original TimingPathDB when supplied.
Each partition applies `set_input_delay -max` and `set_output_delay -max` only
to its surviving original ports and their declared DUT clock root. Vector
ports bind all their bits. Delay values remain in nanoseconds when the DUT
implementation period changes; they are not multiplied by the period scale.
A retained port with the wrong direction or a missing declared clock root is
an error. No environment is inferred for omitted `timing_io`; serial PHY pads
remain governed by their native link contract. These are source setup I/O
constraints, distinct from future per-cut timing-budget feedback. Physical
worker/assembler identities include the environment hash; hold I/O analysis
is outside this setup-only contract.

Native-word synthesized continuations additionally compare the complete
preparation's file population, native IP configurations, pin/XDC sources,
logical bindings and startup-service contract. Both saved preparation seals
must exist; a changed or missing native source rejects continuation. The common
mapped netlist/function and checkpoint checks still apply.

The word physical qualification explicitly retains a routed debug DCP after
route/DRC checks and before timing exports alter delay constraints. The original
clock/margin environment is therefore available to the existing standalone
checkpoint timing exporters. A checkpoint alone is not a timing acceptance
result; infrastructure checks and complete source-bound exports must still
pass. The indexed API's `retain_routed_checkpoint` is false by default and is
also enabled for word-mode Phase 7B workers while independent Phase 7C extraction
is being integrated. This is active-run scratch retention. Remove this
replay input when the active investigation finishes, retaining compact evidence.

The current fixed differential adapter supplies IBUFDS/OBUFDS boundaries; it
does not implement the serializer/deserializer or forwarded-clock circuitry
of the [8:1 serial PHY reference](HARDWARE_PLATFORM.md#serial-phy-time-units).
Its fabric-cycle slot counter must not be interpreted as proof of the serial
UI or the physical DUT clock period. Clock constraints must follow the actual
clock/state-advance circuit; a runtime frame duration alone does not establish
that circuit. Replacing the DUT period with the original target also does not
by itself qualify the transport and clock model.

The indexed implementation currently applies zero input/output delay relative
to the fabric/TDM clock on fixed differential data pads after `synth_design`.
Synthesis therefore does not receive those pad-delay settings, or the required
upstream arrival and downstream timing budgets. The zero setting is a local
measurement reference, not a source-synchronous PHY input window or a
schedule-derived DUT partition arrival budget. The generated timing XDC does
not propagate upstream routed logic and resolved transport arrivals into
partition implementation constraints. A zero-delay pad constraint therefore
does not establish a fully constrained board or DUT implementation.

The corrected boundary contract must distinguish physical LVDS ports from
logical partition interfaces. Physical pad constraints need minimum/maximum
delays relative to the bound forwarded RX/TX clocks. Logical cut inputs need
upstream arrival events, and cut outputs need downstream required-time
budgets, expressed against the original DUT clock relationships. An interface
already implemented as internal transport registers is not a top-level port:
its budget needs the corresponding internal timing objects rather than an
invalid `set_input_delay` on a pin. Original external DUT inputs require their
design/environment constraints. Link delay and upstream arrival must not be
counted again as a PHY pad delay.

Partitions can share the same original DUT clock periods while requiring
different boundary constraints. For a cut signal entering a partition, its
arrival depends on the preceding routed logic and resolved link events;
an output's required time depends on the remaining downstream path and the
original capture edge. For subsequent budget-feedback optimization, these source-bound budgets
must be generated per partition and applied to synthesis as well as implementation. After routed
delays are available, global timing must recheck the complete paths and update
budgets when needed. Independent local timing passes do not prove that the
composed original path meets its clock requirement.

The exporter declares actual SQL net ordinals, including one-based or sparse
keys. Disconnected input identities start beyond the maximum source ordinal,
so they cannot alias a real net. Generated RTL disables implicit net creation.
Before mapping, a bounded SQLite audit rejects every used net without a driver,
including source-declared `undriven` nets, and saves a compact diagnostic sample.
An upstream semantic repair or an explicitly specified OOC input model is
required for these sources; the provider never invents constant bindings.

Native timing bindings include FF clock-enable and reset/set controls, both
LUT6_2 outputs, and the qualified RAMB18 physical write-enable aliases.

The provider uses the Phase 6 sealed differential adapter and XDC through a
physical wrapper. Post-route checks require exact P/N package pins,
IOSTANDARD, IBUFDS/OBUFDS ownership (including Vivado's native DIFFINBUF/IBUFCTRL
decomposition), zero unrouted nets, and zero blocking DRC violations.
Mapped primitives carry source tags, without global KEEP/DONT_TOUCH, LOC/BEL,
or implicit pblocks. Synthesis and physical optimization may remove, merge,
replicate, or change cells. Indexed inventories record observed source lineage
and unretained source tags; this census does not certify functional equivalence.
The explicit `vivado-optimized-source-lineage-v1` policy separates this source
census from actual routed physical components and inserted infrastructure.

The current census protocol explicitly records native primitive level and,
for physical leaf/internal objects, site, BEL, tile column and tile row. Macro
wrappers remain lineage observations but do not occupy a second BEL. Tile
coordinates use [the shared RapidWright/Vivado column/row definition](https://www.rapidwright.io/javadoc/com/xilinx/rapidwright/device/Tile.html),
not the numbers embedded in site names or OpenPARF's site-stride x encoding.
The native importer validates unique site/BEL occupancy and complete placement
for routed physical objects, checks source origins against the mapped owner,
and atomically publishes logical `emuflow.vivado-cell-lineage/v2`. The retained
SQLite owner contains both observed provenance and placement facts. The native
session retains it after exporting timing. Physical acceptance checks its path,
byte count, digest and unchanged source generation, and publishes it as an
artifact for later placement consumers; retiring raw censuses must not delete
this owner. Objects
reference integer site IDs; each site's native coordinates are stored once,
rather than repeated for every packed cell. There is no separate full placement
JSON. Tcl caches geometry only for sites reached in the frozen census. Its placed-object count must agree with the
independent physical mapped-cell census. Consumed raw pre/post-route TSV
snapshots are removed after import; their byte hashes remain in the owner.
These are placement/provenance checks, not a device-geometry or functional
proof. Source tags that disappear remain explicitly unretained; no zero-delay
or fabricated-location fallback is introduced. The Chimew lookahead queries
this same owner by source identity; missing active source/boundary placement
remains an error.

Timing queries resolve the original source's typed descendants first, without
broadening a retained source to other equivalent cells. A removed object may
alias another source only when the primitive, parameters, pin interfaces and
original input connections are identical. For FDRE/FDSE/FDCE/FDPE only, an
absent source may additionally associate through duplicated single LUT1
drivers on CE/reset/set controls. The LUT1 parameters and input identities must
match exactly with fully specified INIT; the register type, parameters, pin
interfaces, clock and D identities remain exact. This bounded structural rule
does not admit retiming, unknown control functions, or a deletion proof.
Queries across descendant collections
take the worst physical endpoint delay. Removed source objects require checked
elimination proofs before an original path can be marked optimized away.
Unproved missing associations and unsupported transformations fail timing
coverage; they never trigger blanket endpoint locks, silently drop original
paths, or qualify incomplete global WNS/TNS. Fixed I/O
and the complete original TimingPathDB remain independently checked.
Collections with multiple physical endpoints are labelled
`optimized-endpoint-collection-upper-bound`, rather than endpoint-exact.
Sequential transformations whose cycle correspondence is not established
are rejected by the timing adapter, without disabling the vendor optimization.
The indexed elimination-proof owner records closed source regions with no path
to a retained source or partition output, and inductively constant register
states with specified initial values. A proof that depends on selecting a
don't-care initial value remains a conditional candidate until the vendor's
choice is confirmed; it cannot eliminate a timing record on its own.
Unknown combinational behavior, changing reset values,
and unsupported primitives do not establish a constant proof. Timing records
must reference this canonical source proof rather than invent a zero delay.
Source names are sealed with the proof so a timing record cannot borrow an
unrelated cell's elimination. A routed proof is bound to one FPGA and mapped
input; a synthesis-stage diagnostic cannot qualify a routed timing disposition.

Local timing storage version 2 retains every original local member, including
`measurement: optimized-away`, `delay_ns: null`, and an `optimization_source`
reference when its requested endpoint has a checked elimination proof. It does
not invent a physical endpoint trace. Version 1 measured-only generations remain
readable. The query resolver, Tcl export, and importer preserve the disposition;
the physical summary references one compact proof database per FPGA. Unknown
initialization candidates remain unresolved until the vendor choice is confirmed.
The boundary-timing and cross-layer physical-binding JSON schemas also encode
null-only elimination records and reject fabricated numerical delays.
Logic-segment and boundary timing storage version 2 provide the same nullable
disposition contract and retain measured-only version 1 readers. A removed shadow
interface must reference its canonical Phase6 boundary register; a different
cell's proof is rejected. An unassociated DUT net or port is still unresolved.
Cross-FPGA binding consumes every original logic segment and scheduled boundary,
then excludes an original member from numerical STA only when a required segment
or interface has a checked elimination proof. Missing segments, events, interfaces,
and conditional initialization candidates still make qualification incomplete.
The global OpenSTA engine consumes this population contract; the retired Python
production composer is not a fallback.
Whole-device resource budgets also apply to the final physical cell population.
Resource accounting excludes MACRO wrappers while retaining LEAF and INTERNAL
implementation components. LUT capacity is counted by occupied site/LUT letter
(O5/O6 share one LUT); DSP and URAM components are counted by unique sites.

Same-FPGA timing queries cover the complete declared original-path population;
cross-FPGA logic and interface queries retain the Phase 6 identities. Fixed
logical transport ports resolve to the declared physical P pad. Query extents
are checked against actual timing endpoints; a sequential Q launch may start
at the same primitive's clock to include clock-to-Q, while an unrelated prefix
or any suffix beyond the requested endpoint is rejected. After implementation
reports are finalized, the routed snapshot receives equal datapath-only
requirements across clock pairs to remove nominal clock-period bias. Vivado
still ranks data delay plus the endpoint library check; the selected path's
`DATAPATH_DELAY` is not independently proven to be the largest raw delay across
endpoint aliases or different capture transitions. This measurement
constraint is excluded from implementation WNS/TNS and global event deadlines. The
indexed provider does not use the legacy sampled Vivado TimingPathDB as terminal
evidence. Per-FPGA vendor WNS/TNS describe vendor endpoint constraints; global
original-path WNS/TNS still require the separate complete OpenSTA composition.

Use an installed Vivado with the exact part database and a valid synthesis and
implementation license. `EMUFLOW_VIVADO_THREADS` controls the recorded thread
limit (default 2). All generated files, checkpoints, and temporary tool files
belong to the selected output directory and experiment scratch lifecycle.
`scripts/validation/qualify_vivado_indexed.py` exercises a small three-clock,
four-differential-pair implementation on XCVU19P. Its default `Quick` placement
directive is specific to this smoke test; the production provider remains
`Default`. Its result explicitly covers
synthetic physical/provider qualification only. `--exercise-continuation` also
checks implementation from the same synthesized snapshot in a fresh attempt.

The indexed provider `indexed-phase6-vivado-physical-v2` uses one native design
session. Between stages, short Python children use SQLite indexes to classify
all required endpoints, plan complete conditional-state frontiers, confirm the
native facts, and resolve the final routed queries. Source indexes close before
the next native algorithm or timing export proceeds. The version 2 session
manifest explicitly selects the `optimized` preplacement snapshot. After
`opt_design`, a fresh census identifies all required endpoints that optimization
removed. Unknown sources without supported associations or checked constant
candidates fail before `place_design`. Complete constant frontiers are observed
and confirmed in that same optimized design; the version 3 confirmation names
its observation stage and binds its inventory as well as the input synthesized
checkpoint. Synthesis-only diagnostic snapshots remain explicitly labelled and
cannot be substituted for an optimized observation.
After routing, every query still requires a routed association or proof.

The native session finalizes route status, DRC, utilization and vendor timing
reports before exporting boundary/local/logic delays from its current routed
design. It validates exact clocks and fixed differential pads and rejects
undeclared placement regions. Current-design exporters require the matching
native snapshot stage. A synthesized checkpoint binds constant-selection facts;
placed and routed checkpoints are not mandatory intermediates. Consumed lineage
and resolved-query scratch is retired between stages; imported timing records
and proof stores retain the canonical facts.

Within the same active experiment, `run_indexed_vivado_partition` accepts
`synthesized_checkpoint`, `synthesized_checkpoint_sha256`, and
`continuation_source_report` together. Use a fresh native output directory.
Admission verifies the checkpoint, mapped/function/Verilog identities, frozen
runtime XDC and fixed-I/O wrapper/query contracts; Vivado additionally checks
the loaded part and `TOP` module property, clocks and full post-route
electrical/coverage gates. The design `NAME` after reopening is a checkpoint
container label and does not identify the HDL top module.
Continuation runs opt/place/physical optimization/route from that synthesis
boundary. This interface does not publish a persistent checkpoint cache.

`scripts/vivado/inspect_checkpoint_identity.tcl INPUT_DCP` reports native design
and fileset identity properties without implementation or checkpoint writes.

An explicit checkpoint I/O diagnostic is available as
`scripts/vivado/inspect_checkpoint_io.tcl INPUT_DCP OUTPUT_DIRECTORY EXPECTED_PART`.
It opens and writes a temporary checkpoint and checks its part; this is an I/O
check and does not qualify placement, routing, or numerical timing.

`scripts/vivado/inspect_timing_points.tcl INPUT_DCP OUTPUT_DIRECTORY ?MODE?`
records one worst path's native timing properties and report. `original` keeps
the checkpoint constraints; `datapath-only` applies the same uniform data-path
requirements used by delay exporters. `clock-aware-reference` uses the uniform
reference while retaining native clock and capture-check adjustments. Both
reference modes record requirement minus slack as `reference_cost_ns`, its
signed difference from `DATAPATH_DELAY`, and the selection policy. A favorable
clock adjustment may be negative and must not be clamped or called raw delay.
The mode is recorded with the properties.
These snapshot queries use a common 1 ns reference requirement to avoid loss
of floating-point precision from a very large requirement. Negative query
slack is allowed; this reference is not a DUT/TDM clock or implementation
closure target. Original implementation reports precede these query overrides.
This read-only diagnostic helps distinguish clock/skew/check adjustments from
raw data delay; it is not a timing-result producer or acceptance certificate.

For a bounded endpoint-query qualification, `export_logic_segment_timing.tcl`
accepts a sixth argument `clock-aware-reference` after the hierarchy and
fixed-I/O-map arguments. It appends `reference_cost_ns` and the signed
`check_clock_adjustment_ns` to the ordinary eight-column query result. Missing
common-reference requirements fail the query, including ignored paths that do
not expose a usable slack. Optimized-away records keep both values absent.
This optional format is diagnostic input for sampling-cost qualification;
the production eight-column importer does not yet consume it. The maximum
effective sampling cost and its associated raw delay are separate quantities.

`scripts/vivado/inspect_constant_frontiers.tcl INPUT_DCP QUERY_TSV OUTPUT_TSV`
exports selected LUT INIT values and every input driver with its source tag.
The query has `source_origin` and `native_cell_hex` columns. The bounded
`require_equal_frontier_functions` checker exhausts the union of input variables
for one source/native LUT pair, allowing pin permutations and reduced LUTs only
when their complete functions agree. Matching a pin name or observing a GND pin
alone is insufficient. These diagnostic facts do not automatically promote an
unknown-initialization candidate to a routed elimination proof.

`compare_selected_constant_frontiers` joins these observations to the mapped
source and its primitive-signature index. It checks every selected object and
input pin, identifies live variables by source output provenance, and rejects
unsupported sequential transformations. Selection is capped at 4096 objects;
the diagnostic reconstructs only one LUT pair at a time. Conditional candidate
values remain hypotheses in this comparison and still need complete fanout
coverage before they can become elimination proofs.

`prepare_constant_frontier_query` follows state-to-state fanouts through
conditional candidates to construct a bounded closed component. If induction
requires other conditional state values, it expands the component and checks
all their fanouts too; it never borrows their hypothetical values silently.
It proves the expanded joint state values inductively and returns the native LUT inspection
query. It closes the source databases before the native checkpoint is opened;
the plan has `status: inspection-required` and is not an elimination proof.
`requested_sources` identifies the input roots; `sources` explicitly lists
every state in the expanded component.
The component is limited to 4096 states, 4096 retained LUT objects, 65536 fanout
uses and frontier references, and a bounded closure traversal.

`confirm_selected_constant_sources` rebuilds that plan and checks complete
fanout coverage for the closed component. Every use must stay inside the
independently inductive selected state set or reach a checked retained LUT, and
every native replica must be observed. Each source record lists all reachable
external LUT frontiers, including exits through other selected states;
components without an observed frontier remain unresolved. Likewise, a direct
output port or unsupported fanout remains unresolved. The certificate binds the FPGA, mapped source,
conditional source-model artifact, synthesized checkpoint, source inventory,
and native observations. Its explicit policy chooses a legal joint
initialization for unspecified states and confirms the selected substitutions
at their retained frontiers. It is not equivalence for every initial state.
A routed consumer must still check its synthesized checkpoint identity and
import the certificate into the partition proof owner before global timing may
use these selected states as elimination proofs.

The proof owner accepts `CheckedConstantSources` from
`check_constant_sources`, rather than a saved JSON `status: pass`. It binds the
checked facts to its own FPGA, mapped index, and synthesized checkpoint, verifies
the selected values against its current inductive model, and seals the compact
confirmation document inside the partition proof database. Only those selected
sources can gain `initialization: vendor-confirmed`; other conditional states
remain unproved. Proof readers verify the document seal, complete frontier
references, selected values, policy, and counts. A preplacement confirmation
must explicitly select its synthesized or optimized observation stage; same-stage
proofs must match that inventory exactly. Neither preplacement source model is
accepted as a routed elimination proof. The routed owner rechecks source closure
and inductive constants against its own observed census. The native session recreates the checked facts
from their bound inputs at the routed preparation boundary and imports them
into the canonical partition proof owner before any timing skip is exported.

Endpoint association uses the indexed source-origin lookup for retained cells.
Only absent origins enter the indexed identical-input signature lookup. Both
branches use the stage/origin index explicitly, so a pin query cannot become a
scan of the full physical inventory. The lineage snapshot is immutable during
query consumption. The structural control fallback uses indexed D-net fanout
and rejects candidate or physical-object collections above 4,096; bounded
operation-local caches retain no whole-circuit graph. No SQLite integrity audit
is needed for resolution.

An explicit `diagnostic_unresolved_path` on `resolve_timing_query` scans the
whole requested query population and aggregates unresolved required sources by
source pin and role into a bounded-memory SQLite diagnostic. It reports
`incomplete` when any required use is unresolved. Optional unused cone anchors
remain deferred. Diagnostic TSV rows use an invalid native hint, so ignoring
the incomplete status cannot silently produce partial timing measurements.
Normal execution retains fail-fast behavior.

Constant-confirmation version 2 proves the selected states inductively using
only their selected values and specified, already-proven constant states. Other
conditional source states remain unknown; their proposed INIT choices cannot be
borrowed silently. For the selected synthesis frontiers, primitive type, INIT,
and each pin binding must also agree with the source projection. Boolean
function equality alone can conceal a different input association and is only
a diagnostic in that case. These checks validate selected synthesized facts;
Vivado optimization remains unconstrained. Version 1 confirmation documents are
preliminary diagnostics and cannot qualify a vendor-confirmed elimination in
the current proof reader.
