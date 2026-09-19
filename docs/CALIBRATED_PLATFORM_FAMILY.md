# Calibrated platform family

EmuFlow supports differently sized designs through a catalog of explicitly
observed platform specifications. It does not resize a board graph or infer
links from an FPGA count.

## Admission boundary

Every selectable specification binds:

1. one sealed `emuflow.calibrated-academic-platform/v2` model whose fitted
   reference interval contains the declared capacity of one named physical
   FPGA on every resource axis;
2. one configuration already enumerated by that model;
3. one conservative, nominal, or aggressive uncertainty profile;
4. one explicit utilization limit no greater than the calibrated model limit;
5. a passing disjoint microbenchmark holdout;
6. a passing free-partition application holdout at that utilization limit; and
7. a fresh one-shot Phase 1--7 acceptance at the same utilization limit with physical seed 1, macro-cycle
   equivalence, schedule legality, zero DRC violations, zero unrouted nets,
   complete timing-path coverage, and independent system-global OpenSTA.

The full-flow artifact must be produced with
`emuflow platform calibrated-full-flow-acceptance`; it is reconstructed from
the canonical flow/QoR reports rather than filled in by an experiment script.
The compact artifact seals both source report hashes and verifies the physical
report's recorded seed.

A specification is `candidate` until all evidence exists. Candidates are never
eligible for selection.

Once all evidence is present, use
`emuflow platform calibrated-family-qualify --spec ... --output-dir ...` to
assemble the family. The operation copies only compact evidence into a new
directory, fills all SHA-256 identities itself, validates every tier and the
cross-tier monotonic-capacity rule, and publishes the directory atomically.
It never converts an incomplete candidate into a qualified tier.

Specifications are calibrated independently. A family loader never infers that
different FPGA counts share the same device capacity, link service, or loading
policy. Every qualified specification must use a distinct calibrated model
artifact and independent evidence. A sealed reference identity may justify
equal fitted values for two known-identical device/link classes, but it does not
permit reusing one model artifact across FPGA counts.

Device resource-unit mapping probes are the one intentionally shared
calibration input when tiers use the same reference device class and the same
academic mapper. They compare identical isolated RTL in the two resource
namespaces and therefore do not characterize board topology. Their original
probe configuration remains recorded as provenance; capacity boundaries,
links, routes, and delays must still be measured independently for every tier.

A two-FPGA point-to-point tier cannot identify separate endpoint and per-hop
delay terms because every legal path has exactly one hop. EmuFlow fits the
observable one-hop total directly and seals that model as one-hop-only; it
rejects any attempt to use the model for a multi-hop prediction. Larger tiers
must independently vary hop count so the two terms remain identifiable.

## Selection

The design demand contains the resource counts produced by the selected open
mapping flow. For each qualified tier, EmuFlow applies the tier's admitted
utilization limit to each per-FPGA academic mapper-equivalent capacity,
computes the number of FPGAs
required independently for each resource, and chooses the lowest service rank
whose explicit configuration has enough devices. Qualified ranks must increase
capacity in every resource dimension, which prevents a guessed scalar score
from comparing incomparable devices.

This is an aggregate prefilter only. A selected topology can still fail because
resources cannot be balanced or because its links cannot service the realized
cut. Normal Phase 3 capacity checks and the calibrated post-partition
communication-envelope gate therefore remain mandatory.

Selection reports retain all three units: the named physical-device capacity,
the reference-flow capacity, and the effective academic mapper-equivalent
capacity used by the prefilter. The last value is never a physical LUT count.

## Publication boundary

The reusable selector, validators, schemas, and synthetic tests are public.
Reference-flow paths, raw reports, vendor files, credentials, and non-authorized
numeric observations remain outside Git. A calibrated family is an academic
behavioral model and is not a hardware clone.
