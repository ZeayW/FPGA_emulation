# PATRON Static Exact four-arm baseline-provider result

## Question and scope

This record closes the previously pending four-arm diagnostic for clean-start
PATRON v6, clean-start PATRON v9, and one physical-feedback v11 descendant of
each parent.  Every arm runs the complete Phase 1--7 path on Koios DLA medium,
EDA 2023 case 6; Phase 3 proxy metrics alone are not used as the result.

This is a controlled **baseline Phase 6 provider**, physical-seed-1,
single-worker experiment.  It is not the repository's full
baseline/placement-aware/Chimew provider-by-seed qualification matrix, and it
does not replace the separate Chimew/eight-worker V11 result documented in the
main partitioning upgrade plan.

## Frozen controls

| control | value |
|---|---|
| case | `koios-dla-medium-l5__eda2023-case6` |
| design / platform | `DLA` / `eda2023-case6-rtl` |
| cut mode | Static Exact combinational |
| Phase 6 provider | baseline |
| physical seed / workers | 1 / 1 |
| route channel width | 300 |
| EmuIR SHA-256 | `cb6c74059d40279442b5c1c2f272fea6ad5ea7a1e7c1084f9bfe402ebcae5e99` |
| original timing paths | 195,532 |
| original path-ID SHA-256 | `a2fb7551b567957f38fe4159605886bee5ee60d861875cd1cdc8a2fc215bba8b` |

The recovered cold arms use source commit
`512ad2f1443a9cd2d0d27234f063d32a2cd29e59`.  The feedback arms use commit
`2e986c9ee056f7fbb1129f86e45dc49d6dc9e2a6`, retained in the own-fork tag
`patron-static-exact-four-arm-20260902`.  The latter includes the exact
reentrant-path handling required by the observed full-size feedback input.

## Complete Phase 7 result

| arm | target WNS (ns) | target TNS (ns) | negative paths | physical WNS (ns) | unrouted / DRC |
|---|---:|---:|---:|---:|---:|
| v6 | -95.390825202 | -264,633.1135519021 | 6,271 | 7.02073 | 0 / 0 |
| v9 | -95.390825202 | -264,633.1135519021 | 6,271 | 7.02073 | 0 / 0 |
| v6 to v11 | -97.289380855 | -260,656.0599069793 | 6,051 | 10.1567 | 0 / 0 |
| v9 to v11 | -97.289380855 | -260,656.0599069793 | 6,051 | 10.1567 | 0 / 0 |

All four arms independently cover 195,532/195,532 original timing paths.  No
arm uses a fallback path or a discontinuous compressed path.  All four physical
flows pass with zero unrouted nets and zero DRC violations.

The two cold-start versions are physically identical under these controls.
Their two feedback descendants are also physically identical.  Relative to
either parent, v11:

- reduces the negative-path count by 220;
- improves target-clock TNS by 3,977.053644922824 ns, a 1.5028556296462159%
  deficit reduction; and
- worsens target-clock WNS by 1.8985556530000025 ns, a 1.9902916753048454%
  deficit increase.

The result therefore **fails the declared dual-metric promotion gate**.  It is
a mixed, useful negative result: the feedback objective improves aggregate
negative slack and path count, but moves the worst path in the wrong direction
under this baseline-provider configuration.  It must not be summarized as an
unqualified V11 QoR improvement.

## Feedback reconstruction diagnostic

The independently checked v6-to-v11 Phase 3 trace contains 317 moves.  Its
physical-feedback reconstruction covers all 195,532 source paths, including
6,127 cross-FPGA paths and 5,811 positive endpoint-pair residuals.  One exact
path leaves an FPGA and returns to the same endpoint FPGA.  The endpoint-pair
model correctly reports it as `endpoint_pair_ineligible_cross_paths=1` rather
than fabricating a distinct source/sink residual; source coverage, assignment,
and transition checks remain intact.

## Evidence and reproducibility

The machine-readable result is
[`benchmarks/results/patron_static_exact_four_arm_case6/result.json`](../benchmarks/results/patron_static_exact_four_arm_case6/result.json).
Each arm includes its independently validated compact Phase 7 report and the
SHA-256 manifest for every retained full-output file.  The two feedback Phase 7
archives are additionally bound by these archive identities:

| arm | archive bytes | archive SHA-256 |
|---|---:|---|
| v6 to v11 | 343,798,596 | `fb69a0bdcff9f18124be0717e5e84ebc37b327ba1d6e2b17c4c05d73b901c8aa` |
| v9 to v11 | 343,808,333 | `f198dd971effad98ea7dfcb1f96cf87062b337d4898b857500351399cac32ed1` |

Large routed outputs remain outside the source repository.  The checked-in
reports and manifests contain no substituted metrics: they preserve the final
validator projections and content hashes needed to identify those outputs.

## What this does not establish

- It does not qualify the complete provider-by-seed validation matrix.
- It does not establish topology generality beyond EDA 2023 case 6.
- It does not compare different worker counts or route-channel settings.
- It does not contradict the separate Chimew/eight-worker V11 gate because the
  Phase 6 provider and physical execution controls differ.

Those extensions are follow-on experimental decisions, not prerequisites for
recording this completed four-arm result.
