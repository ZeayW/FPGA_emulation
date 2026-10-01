"""Provider-neutral microbenchmarks for PPro black-box calibration.

The probes use public RTL inference attributes only.  Their requested unit
counts are experiment controls, not claims about mapped resources; ordinary
PPro resource reports remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

from .errors import ValidationError
from .io import write_json
from .ppro_blackbox_runner import MOCK_REPORT_PROFILE, RUN_SPEC_SCHEMA, validate_run_spec


CAPACITY_AXES = {
    "bram",
    "dsp",
    "ff",
    "lut",
    "mixed_bram_dsp",
    "mixed_lut_ff",
    "uram",
}
_GENERATOR_VERSION = "ppro-blackbox-capacity-probe-v1"
_GENERATOR_REVISION = hashlib.sha256(_GENERATOR_VERSION.encode("utf-8")).hexdigest()
_EXPECTED_REPORTS = [
    "partition_summary",
    "resource_summary",
    "route_summary",
    "system_timing",
]


@dataclass(frozen=True)
class CapacityProbeBundle:
    root: Path
    rtl_path: Path
    filelist_path: Path
    parameters_path: Path
    constraints_path: Path
    run_spec_path: Path
    run_spec: Dict[str, Any]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _probe_body(axis: str, units: int) -> str:
    if axis == "lut":
        return f"""
    (* keep = "true", dont_touch = "yes" *) wire [{units - 1}:0] lut_nodes;
    genvar gi;
    generate for (gi = 0; gi < {units}; gi = gi + 1) begin : g_lut
        assign lut_nodes[gi] = (stimulus[(gi + 0) % 64] & stimulus[(gi + 7) % 64]) ^
                               (stimulus[(gi + 13) % 64] | stimulus[(gi + 29) % 64]) ^
                               stimulus[(gi + 47) % 64];
    end endgenerate
    assign digest = ^lut_nodes;
"""
    if axis == "ff":
        return f"""
    (* keep = "true", dont_touch = "yes", shreg_extract = "no" *) reg [{units - 1}:0] ff_bank;
    always @(posedge clk) begin
        if (!reset_n) ff_bank <= {{{units}{{1'b1}}}};
        else ff_bank <= {{ff_bank[{units - 2}:0], ff_bank[{units - 1}] ^ ff_bank[0] ^ stimulus[0]}};
    end
    assign digest = ^ff_bank;
"""
    if axis in {"bram", "uram"}:
        style = "block" if axis == "bram" else "ultra"
        depth = 1024 if axis == "bram" else 4096
        return f"""
    localparam integer MEMORY_DEPTH = {depth};
    wire [11:0] address = stimulus[11:0];
    wire [31:0] write_data = stimulus[31:0] ^ stimulus[63:32];
    wire [{units - 1}:0] memory_digest;
    genvar gi;
    generate for (gi = 0; gi < {units}; gi = gi + 1) begin : g_memory
        (* ram_style = "{style}", keep = "true", dont_touch = "yes" *)
        reg [31:0] memory [0:MEMORY_DEPTH-1];
        reg [31:0] read_data;
        always @(posedge clk) begin
            if (stimulus[63]) memory[address % MEMORY_DEPTH] <= write_data ^ gi;
            read_data <= memory[(address + gi) % MEMORY_DEPTH];
        end
        assign memory_digest[gi] = ^read_data;
    end endgenerate
    assign digest = ^memory_digest;
"""
    if axis == "dsp":
        return f"""
    wire [{units - 1}:0] dsp_digest;
    genvar gi;
    generate for (gi = 0; gi < {units}; gi = gi + 1) begin : g_dsp
        (* use_dsp = "yes", keep = "true", dont_touch = "yes" *)
        wire [35:0] product = (stimulus[17:0] + gi) * (stimulus[35:18] ^ gi);
        assign dsp_digest[gi] = ^product;
    end endgenerate
    assign digest = ^dsp_digest;
"""
    if axis == "mixed_lut_ff":
        return f"""
    (* keep = "true", dont_touch = "yes", shreg_extract = "no" *) reg [{units - 1}:0] mixed_state;
    wire [{units - 1}:0] mixed_lut;
    genvar gi;
    generate for (gi = 0; gi < {units}; gi = gi + 1) begin : g_mixed
        assign mixed_lut[gi] = mixed_state[gi] ^ stimulus[gi % 64] ^
                               (stimulus[(gi + 11) % 64] & stimulus[(gi + 37) % 64]);
    end endgenerate
    always @(posedge clk) begin
        if (!reset_n) mixed_state <= {{{units}{{1'b0}}}};
        else mixed_state <= mixed_lut;
    end
    assign digest = ^mixed_state;
"""
    if axis == "mixed_bram_dsp":
        return f"""
    wire [{units - 1}:0] mixed_digest;
    genvar gi;
    generate for (gi = 0; gi < {units}; gi = gi + 1) begin : g_mixed_hard
        (* ram_style = "block", keep = "true", dont_touch = "yes" *)
        reg [35:0] memory [0:511];
        reg [35:0] read_data;
        (* use_dsp = "yes", keep = "true", dont_touch = "yes" *)
        wire [35:0] product = (stimulus[17:0] + gi) * (stimulus[35:18] ^ gi);
        always @(posedge clk) begin
            memory[(stimulus[8:0] + gi) % 512] <= product;
            read_data <= memory[stimulus[8:0]];
        end
        assign mixed_digest[gi] = ^read_data;
    end endgenerate
    assign digest = ^mixed_digest;
"""
    raise ValidationError(f"unsupported capacity axis {axis!r}")


def _capacity_probe_rtl(axis: str, units: int) -> str:
    return f"""// Generated by {_GENERATOR_VERSION}; requested units are controls, not mapped counts.
module ppro_blackbox_capacity_probe (
    input  wire        clk,
    input  wire        reset_n,
    input  wire [63:0] stimulus,
    output wire        digest
);
{_probe_body(axis, units)}
endmodule
"""


def generate_capacity_probe_bundle(
    output_dir: Path,
    *,
    axis: str,
    units: int,
    repeat: int,
    role: str,
    campaign_id: str,
    public_prior_id: str,
    configuration_id: str,
    tool_release: str,
    runner_revision: str,
    seed: int,
) -> CapacityProbeBundle:
    if axis not in CAPACITY_AXES:
        raise ValidationError(f"capacity axis must be one of {sorted(CAPACITY_AXES)}")
    for name, value, minimum in (
        ("units", units, 2),
        ("repeat", repeat, 0),
        ("seed", seed, 0),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValidationError(f"capacity {name} must be an integer >= {minimum}")

    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    rtl_path = root / "capacity_probe.v"
    filelist_path = root / "sources.f"
    parameters_path = root / "parameters.json"
    constraints_path = root / "documented_constraints.json"
    run_spec_path = root / "run-spec.json"
    rtl = _capacity_probe_rtl(axis, units)
    rtl_path.write_text(rtl, encoding="utf-8")
    filelist_path.write_text("capacity_probe.v\n", encoding="utf-8")
    parameters = {"axis": axis, "generator": _GENERATOR_VERSION, "units": units}
    constraints = {
        "assignments": [{"partition": "P0", "target": "F0"}],
        "control_mode": "fixed_assignment",
        "documented_actions": ["partition_constraint", "random_seed"],
        "seed": seed,
    }
    write_json(parameters_path, parameters, compact=True)
    write_json(constraints_path, constraints, compact=True)
    case_id = f"capacity-{axis}-u{units}-r{repeat}"
    spec = {
        "schema": RUN_SPEC_SCHEMA,
        "identity": {
            "id": f"{campaign_id}.{case_id}",
            "campaign_id": campaign_id,
            "case_id": case_id,
            "role": role,
            "public_prior_id": public_prior_id,
            "configuration_id": configuration_id,
        },
        "tool": {
            "name": "PPro",
            "release": tool_release,
            "runner_revision": runner_revision,
        },
        "workload": {
            "generator_id": f"ppro-blackbox-capacity-{axis}-v1",
            "generator_revision": _GENERATOR_REVISION,
            "rtl_sha256": _sha256(rtl.encode("utf-8")),
            "parameters_sha256": _sha256(_canonical(parameters)),
            "design_metrics": {"requested_units": units},
        },
        "experiment": {
            "kind": "resource_capacity",
            "control_mode": "fixed_assignment",
            "documented_actions": ["partition_constraint", "random_seed"],
            "constraints_sha256": _sha256(_canonical(constraints)),
        },
        "execution": {"seed": seed},
        "adapter": {
            "profile": MOCK_REPORT_PROFILE,
            "expected_reports": _EXPECTED_REPORTS,
        },
    }
    normalized = validate_run_spec(spec)
    write_json(run_spec_path, normalized, compact=True)
    return CapacityProbeBundle(
        root=root,
        rtl_path=rtl_path,
        filelist_path=filelist_path,
        parameters_path=parameters_path,
        constraints_path=constraints_path,
        run_spec_path=run_spec_path,
        run_spec=normalized,
    )


def generate_capacity_matrix(
    output_dir: Path,
    *,
    axes: Sequence[str],
    fit_units: Sequence[int],
    holdout_units: Sequence[int],
    repeats: int,
    campaign_id: str,
    public_prior_id: str,
    configuration_id: str,
    tool_release: str,
    runner_revision: str,
    seed_base: int = 1,
) -> list[CapacityProbeBundle]:
    if repeats < 1:
        raise ValidationError("capacity matrix repeats must be >= 1")
    if set(fit_units) & set(holdout_units):
        raise ValidationError("capacity fit and holdout unit sets must be disjoint")
    bundles = []
    for axis in sorted(set(axes)):
        for role, values in (("fit", fit_units), ("holdout", holdout_units)):
            for units in sorted(set(values)):
                for repeat in range(repeats):
                    case_root = output_dir / axis / f"u{units}" / f"r{repeat}"
                    bundles.append(
                        generate_capacity_probe_bundle(
                            case_root,
                            axis=axis,
                            units=units,
                            repeat=repeat,
                            role=role,
                            campaign_id=campaign_id,
                            public_prior_id=public_prior_id,
                            configuration_id=configuration_id,
                            tool_release=tool_release,
                            runner_revision=runner_revision,
                            seed=seed_base + repeat,
                        )
                    )
    return bundles
