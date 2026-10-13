# RTL benchmarks

[The source catalog](../benchmarks/rtl_catalog.json) records upstream design
revisions, licenses, top modules, source paths, and readiness metadata.
Fetched sources live in the ignored `third_party/rtl/` directory.

List and fetch designs:

```bash
python3 scripts/benchmarks/fetch.py list
python3 scripts/benchmarks/fetch.py fetch picorv32
```

Use a checked-in workload contract with its pinned source checkout:

```bash
python3 scripts/benchmarks/fetch.py fetch secworks_aes
emuflow benchmark benchmarks/runs/secworks_aes_l3.json \
  --source-root third_party/rtl/secworks_aes \
  --out build/secworks-aes
```

A single-workload benchmark command is not automatically a complete multi-FPGA
physical comparison. Consult its run contract, selected platform, and terminal
validator to identify the completed boundary.

## Workloads and platforms

Real RTL supplies logic, state, clocks, and resource demand. BoardDB supplies
FPGA topology and communication constraints. These are independent inputs;
never treat a contest communication graph as a synthesized circuit.

The catalog includes small RTL such as SERV, PicoRV32, and AES, along with
larger Koios, VeeR, and NVDLA sources. Catalog membership or a pinned source
revision does not establish that every mapping/device/platform combination is
supported. RAM, DSP, carry, wide-mux, and macro constraints must match the
selected physical backend. Do not flatten unsupported hard blocks into soft
logic silently and report unchanged hardware capacity.

Use a naturally connected design for final provider comparisons.
Replicated-core or artificially coupled harnesses may be private stress
fixtures, but are not substitutes for representative full-flow evidence.

## Registries

- [RTL catalog](../benchmarks/rtl_catalog.json): upstream source definitions.
- [Full-flow matrix](../benchmarks/end_to_end_validation_matrix.json):
  real workload/platform combinations and their acceptance contracts.
- [Contest matrix](../benchmarks/contest_validation_matrix.json):
  raw communication-algorithm inputs.

Identify each comparison by its full workload/platform combination, not a bare
contest case number. A registry's planned entry is not a completed result.
A qualified entry needs the declared complete physical and global-timing gates.

## Large generated-RTL candidates

The source catalog registers the following eight configurations from the
external `large-rtl-20260906` collection. Their sizes describe native upstream
configurations, **not measured physical LUT counts or FPGA requirements**.
All eight are `source-candidate-unqualified`; none is added to the full-flow
matrix or promoted by this catalog update.

| Catalog ID | Native configuration / top | Delivered-source clock profile |
| --- | --- | --- |
| `vortex_64core` | 4 clusters × 16 cores, L2; `Vortex` | One root `clk`; preferred single-clock screening candidate |
| `blackparrot_16core` | Official coherent 16-core configuration; `wrapper` | One external `clk_i`, internal divider and programmable RTC clock mux; not strict single-clock |
| `opdb_chip_10x10` | 100-tile 2D mesh; `chip` | Core, IO and JTAG roots, asynchronous FIFOs |
| `opdb_chip_15x15` | 225-tile 2D mesh; `chip` | Core, IO and JTAG roots, asynchronous FIFOs |
| `opdb_chip_20x20` | 400-tile 2D mesh; `chip` | Core, IO and JTAG roots, asynchronous FIFOs |
| `occamy_432core_full` | Upstream full 432-core SoC; `occamy_top` | Core, peripheral and JTAG clocks; RTC signal also needs classification |
| `chipyard_sonicboom_megaboom` | MegaBoomV3Config digital system; `sonicboom_DigitalTop` | Main, debug, JTAG and serial-TL clock inputs |
| `xiangshan_kunminghu_release_25022801` | Official Kunminghu release 2025022801; `XSTop` | Main and JTAG clocks; RTC signal also needs classification |

These are architecture-native configurations with real system interconnect,
not textually replicated test harnesses. Clock profiles are source-inspection
metadata, not an elaborated clock-tree or CDC certificate. A single external
clock port is insufficient when state generates another clock, as in
BlackParrot. Do not merge or delete clock domains to make a run pass.

### Source checkout versus generated delivery

`python3 scripts/benchmarks/fetch.py list` lists the new IDs. The existing
`fetch` command retrieves their pinned upstream repositories. For OPDB, the
specified sparse path includes the official pickled chip configuration.
For generator-based projects, fetching upstream source does **not** generate
the lowered RTL package, fetch all dependency submodules, or reproduce the
delivered compilation environment automatically.

The catalog's `collections` entry identifies the external ZIP by its SHA-256
and root directory; each design's `package` identifies its directory, ordered
`rtl.f` digest, primary RTL digest and native scale. The ZIP and RTL are not
redistributed in this repository. Retain the package's `LICENSES/` and source
notices when obtaining or preparing the input. The primary-file digest is not
a digest of every dependency: the collection ZIP digest binds the entire
delivery, and `rtl.f` supplies the actual compile order.

The pinned XiangShan repository is the generating OpenXiangShan source, while
its package records the separate RTL-release repository and release identifier.
Chipyard records both the generating 1.13.0 source revision and its Lambdalib
revision. Prepared sv2v RTL must not be relabelled as an untouched upstream
Git checkout; the delivered package and source checkout are different inputs.

Package frontend-smoke records establish neither completed target synthesis
nor full-flow correctness. Before adding a runnable qualification contract:

1. Audit the elaborated clocks and bind explicit target periods, IO timing and
   reset/CDC semantics. Do not guess these from port names.
2. Run target-native mapping, preserving supported RAM/DSP macros. Record the
   primitive resource units and mapping options, including unsupported resource
   classes; source size and vendor estimates are not interchangeable with
   mapped physical LUT counts.
3. Select a declared platform from the measured multi-resource demand,
   utilization policy and transport reserve, without enlarging per-device
   capacity or imposing artificial assignments to manufacture crossings.
4. Register the workload/platform acceptance contract and complete physical
   Phase 1–7, functional/equivalence and original-path global-timing gates
   before claiming qualification or QoR.

No guessed clock periods, fabricated utilization results, unverified PPro
resource totals, or successful-full-flow labels are supplied for these entries.

## Outputs

Machine configuration, raw tool products, measured resource counts, QoR tables,
and experiment histories stay outside the repository. Report complete original-
path coverage, mapping/architecture identity, seed, runtime, DRC/unrouted counts,
and target/runtime global WNS/TNS in the terminal comparison summary.

See [validation requirements](VALIDATION.md).
