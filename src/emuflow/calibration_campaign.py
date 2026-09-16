"""Controlled microbenchmark campaigns for academic-platform calibration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .calibrated_platform import (
    OBSERVATIONS_SCHEMA,
    validate_calibration_observations,
)
from .errors import ValidationError
from .io import read_json, write_json


CAMPAIGN_SCHEMA = "emuflow.platform-calibration-campaign/v1"
MANIFEST_SCHEMA = "emuflow.platform-calibration-campaign-manifest/v1"
RUN_RESULT_SCHEMA = "emuflow.platform-calibration-run-result/v1"
_CASE_KINDS = {"capacity_boundary", "link_capacity_boundary", "link_delay"}
_SOURCE_CLASSES = {"authorized_reference_flow", "synthetic_fixture"}
_PUBLICATION_SCOPES = {"internal", "aggregate_only", "public"}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RUN_STATUSES = {
    "pass",
    "capacity_fail",
    "link_capacity_fail",
    "provider_fail",
    "license_fail",
    "transport_fail",
    "execution_fail",
}


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context}: expected an object")
    return value


def _array(value: Any, context: str, *, nonempty: bool = False) -> List[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValidationError(f"{context}: expected a {qualifier}array")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _identifier(value: Any, context: str) -> str:
    result = _string(value, context)
    if _IDENTIFIER_RE.fullmatch(result) is None:
        raise ValidationError(
            f"{context}: expected an identifier containing only letters, digits, '.', '_', or '-'"
        )
    return result


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"{context}: unknown fields {unknown}")


def _positive_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{context}: expected a positive integer")
    return value


def _unique(items: List[Mapping[str, Any]], context: str) -> None:
    identifiers = [_identifier(item.get("id"), f"{context}.id") for item in items]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError(f"{context}: duplicate IDs")


def _percentage(value: Any, context: str) -> int:
    result = _positive_integer(value, context)
    if result > 100:
        raise ValidationError(f"{context}: expected an integer <= 100")
    return result


def _tcl_braced(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValidationError("Tcl path/value must be one line and contain no NUL")
    escaped = value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "{" + escaped + "}"


def _ppro_runner_tcl(
    *,
    case_id: str,
    topology_file: str,
    utilization_limits_percent: Mapping[str, int],
) -> str:
    project_name = _tcl_braced("calibration_" + case_id)
    return f"""# Generated controlled calibration run. Keep raw output outside Git.
set work_space [file normalize [file dirname [info script]]]
set project_parent [file join $work_space ppro_project]
if {{[file exists $project_parent]}} {{
  error "calibration requires a fresh cold-start directory: $project_parent"
}}
file mkdir $project_parent
cd $work_space

set topology_file {_tcl_braced(topology_file)}
if {{[file pathtype $topology_file] eq "relative"}} {{
  if {{![info exists ::env(PPRO_CT_RCF_ROOT)] || $::env(PPRO_CT_RCF_ROOT) eq ""}} {{
    error "relative topology_file requires PPRO_CT_RCF_ROOT"
  }}
  set topology_file [file join $::env(PPRO_CT_RCF_ROOT) $topology_file]
}}
set topology_file [file normalize $topology_file]
if {{![file isfile $topology_file]}} {{
  error "topology_file does not exist: $topology_file"
}}

create_project -project_name {project_name} -project_path $project_parent -force
set_partition_mode -r -d
create_rtlpart

run_compile -top calibration_top -lib work -filelist [file join $work_space filelist.f]
run_pre_partition \\
  -stf $topology_file \\
  -config [file join $work_space prepartition.cfg] \\
  -lut_area {utilization_limits_percent['lut']} \\
  -ff_area {utilization_limits_percent['ff']} \\
  -bram_area {utilization_limits_percent['bram']} \\
  -dsp_area {utilization_limits_percent['dsp']}
run_partition -costmode 1 -max_process_num 4
run_system_route
"""


def _ppro_runner_shell() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PPRO_CT_RCF_ROOT:-}" ]]; then
  echo "PPRO_CT_RCF_ROOT is required" >&2
  exit 2
fi
if [[ ! -x "$PPRO_CT_RCF_ROOT/bin/rtlpart_linux" ]]; then
  echo "rtlpart_linux is not executable below PPRO_CT_RCF_ROOT" >&2
  exit 2
fi

case_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
mkdir -p "$case_dir/tmp"
export TMPDIR="$case_dir/tmp"

# The reference environment owns library and floating-license setup.  Keep it
# outside the campaign and never copy its contents into an EmuFlow artifact.
# Vendor setup scripts commonly probe optional variables before defining them.
# Disable nounset only across that compatibility boundary, then immediately
# restore the launcher's strict mode for the actual run.
set +u
source "$PPRO_CT_RCF_ROOT/setting_rtl.sh"
set -u
if [[ -n "${PPRO_CT_RCF_LICENSE:-}" && -f "$PPRO_CT_RCF_LICENSE" ]]; then
  export s2c_LICENSE="$PPRO_CT_RCF_LICENSE"
fi

set +e
"$PPRO_CT_RCF_ROOT/bin/rtlpart_linux" -script_file "$case_dir/run_ppro.tcl"
runner_rc=$?
set -e
printf '%s\n' "$runner_rc" > "$case_dir/runner.exit-code.tmp"
mv "$case_dir/runner.exit-code.tmp" "$case_dir/runner.exit-code"
exit "$runner_rc"
"""


def validate_calibration_run_result(
    value: Mapping[str, Any], *, expected_case_id: str
) -> Dict[str, Any]:
    root = _mapping(value, "campaign result")
    _reject_unknown(
        root,
        {"schema", "case_id", "status", "controls", "metrics"},
        "campaign result",
    )
    if root.get("schema") != RUN_RESULT_SCHEMA:
        raise ValidationError(
            f"campaign result.schema: expected {RUN_RESULT_SCHEMA!r}"
        )
    case_id = _identifier(root.get("case_id"), "campaign result.case_id")
    if case_id != expected_case_id:
        raise ValidationError("campaign result.case_id: identity mismatch")
    status = _string(root.get("status"), "campaign result.status")
    if status not in _RUN_STATUSES:
        raise ValidationError("campaign result.status: unsupported status")

    controls = _mapping(root.get("controls"), "campaign result.controls")
    _reject_unknown(
        controls,
        {
            "assignment_applied",
            "route_applied",
            "observed_assignment",
            "observed_route",
        },
        "campaign result.controls",
    )
    for field in ("assignment_applied", "route_applied"):
        if not isinstance(controls.get(field), bool):
            raise ValidationError(f"campaign result.controls.{field}: expected boolean")
    assignment = _mapping(
        controls.get("observed_assignment"),
        "campaign result.controls.observed_assignment",
    )
    observed_assignment = {
        _string(instance, "campaign result assignment instance"): _string(
            target, "campaign result assignment target"
        )
        for instance, target in assignment.items()
    }
    raw_route = controls.get("observed_route")
    if raw_route is None:
        observed_route = None
    else:
        observed_route = [
            _string(fpga, "campaign result route FPGA")
            for fpga in _array(raw_route, "campaign result.controls.observed_route")
        ]
        if len(observed_route) != len(set(observed_route)):
            raise ValidationError(
                "campaign result.controls.observed_route: repeated FPGA"
            )

    metrics = _mapping(root.get("metrics"), "campaign result.metrics")
    _reject_unknown(
        metrics,
        {
            "actual_resource_demand_per_fpga",
            "link_line_rate_mbps",
            "link_phy_width_bits",
            "link_channels_per_direction",
            "link_max_tdm_ratio_supported",
            "link_base_route_delay_ns",
            "sr0_worst_cross_fpga_delay_ns",
            "sr0_cross_fpga_path_count",
            "sr0_max_tdm_ratio",
        },
        "campaign result.metrics",
    )
    normalized_metrics: Dict[str, Any] = {}
    for field in (
        "actual_resource_demand_per_fpga",
        "link_phy_width_bits",
        "link_channels_per_direction",
        "link_max_tdm_ratio_supported",
        "sr0_cross_fpga_path_count",
        "sr0_max_tdm_ratio",
    ):
        if field in metrics:
            normalized_metrics[field] = _positive_integer(
                metrics[field], f"campaign result.metrics.{field}"
            )
    for field in (
        "link_line_rate_mbps",
        "link_base_route_delay_ns",
        "sr0_worst_cross_fpga_delay_ns",
    ):
        if field not in metrics:
            continue
        delay = metrics[field]
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or delay <= 0
        ):
            raise ValidationError(
                f"campaign result.metrics.{field}: "
                "expected a positive number"
            )
        normalized_metrics[field] = float(delay)
    return {
        "schema": RUN_RESULT_SCHEMA,
        "case_id": case_id,
        "status": status,
        "controls": {
            "assignment_applied": controls["assignment_applied"],
            "route_applied": controls["route_applied"],
            "observed_assignment": observed_assignment,
            "observed_route": observed_route,
        },
        "metrics": normalized_metrics,
    }


def _capacity_rtl(resource: str, units: int) -> str:
    if resource == "lut":
        declarations = """(* keep_hierarchy = "yes" *)
module calibration_lut_cell(
  input wire [5:0] din,
  output wire dout
);
  assign dout = (din[0] & din[1]) ^ (din[2] | din[3]) ^
                (din[4] & ~din[5]);
endmodule

(* keep_hierarchy = "yes" *)
module calibration_lut_bank #(
  parameter integer WIDTH = 1,
  parameter integer OFFSET = 0
)(
  input wire [63:0] stimulus,
  input wire chain_in,
  output wire chain_out
);
  (* keep = "true" *) wire [WIDTH:0] probe_chain;
  assign probe_chain[0] = chain_in;
  genvar i;
  generate for (i = 0; i < WIDTH; i = i + 1) begin : g_lut
    wire [5:0] cell_input;
    assign cell_input = {
      stimulus[((OFFSET + i) * 13 + 47) % 64],
      stimulus[((OFFSET + i) * 11 + 31) % 64],
      stimulus[((OFFSET + i) * 7 + 23) % 64],
      stimulus[((OFFSET + i) * 5 + 17) % 64],
      stimulus[((OFFSET + i) * 3 + 7) % 64],
      probe_chain[i]
    };
    (* dont_touch = "true", keep_hierarchy = "yes" *)
    calibration_lut_cell u_cell(.din(cell_input), .dout(probe_chain[i + 1]));
  end endgenerate
  assign chain_out = probe_chain[WIDTH];
endmodule

"""
        body = f"""
  localparam integer LUT_BANK_SIZE = 8192;
  localparam integer LUT_BANKS = ({units} + LUT_BANK_SIZE - 1) / LUT_BANK_SIZE;
  (* keep = "true" *) wire [LUT_BANKS:0] bank_chain;
  assign bank_chain[0] = stimulus[0];
  genvar bank;
  generate for (bank = 0; bank < LUT_BANKS; bank = bank + 1) begin : g_lut_bank
    localparam integer THIS_WIDTH =
      ((bank + 1) * LUT_BANK_SIZE <= {units})
        ? LUT_BANK_SIZE : ({units} - bank * LUT_BANK_SIZE);
    (* dont_touch = "true", keep_hierarchy = "yes" *)
    calibration_lut_bank #(
      .WIDTH(THIS_WIDTH),
      .OFFSET(bank * LUT_BANK_SIZE)
    ) u_bank (
      .stimulus(stimulus),
      .chain_in(bank_chain[bank]),
      .chain_out(bank_chain[bank + 1])
    );
  end endgenerate
  assign result = bank_chain[LUT_BANKS];
"""
    elif resource == "ff":
        declarations = """(* keep_hierarchy = "yes" *)
module calibration_ff_bank #(
  parameter integer WIDTH = 1,
  parameter integer OFFSET = 0
)(
  input wire clk,
  input wire [63:0] stimulus,
  output wire reduction
);
  (* keep = "true", dont_touch = "true", shreg_extract = "no" *)
  reg [WIDTH - 1:0] probe;
  integer i;
  always @(posedge clk) begin
    for (i = 0; i < WIDTH; i = i + 1)
      probe[i] <= probe[i] ^ probe[(i + 1) % WIDTH] ^
                  stimulus[(i * 13 + OFFSET + 5) % 64];
  end
  assign reduction = ^probe;
endmodule

"""
        body = f"""
  localparam integer FF_BANK_SIZE = 8192;
  localparam integer FF_BANKS = ({units} + FF_BANK_SIZE - 1) / FF_BANK_SIZE;
  (* keep = "true" *) wire [FF_BANKS - 1:0] bank_reduction;
  genvar bank;
  generate for (bank = 0; bank < FF_BANKS; bank = bank + 1) begin : g_ff_bank
    localparam integer THIS_WIDTH =
      ((bank + 1) * FF_BANK_SIZE <= {units})
        ? FF_BANK_SIZE : ({units} - bank * FF_BANK_SIZE);
    (* dont_touch = "true", keep_hierarchy = "yes" *)
    calibration_ff_bank #(
      .WIDTH(THIS_WIDTH),
      .OFFSET(bank * FF_BANK_SIZE)
    ) u_bank (
      .clk(clk),
      .stimulus(stimulus),
      .reduction(bank_reduction[bank])
    );
  end endgenerate
  assign result = ^bank_reduction;
"""
    elif resource == "dsp":
        declarations = """(* keep_hierarchy = "yes" *)
module calibration_dsp_cell(
  input wire [17:0] lhs,
  input wire [17:0] rhs,
  output wire [35:0] product
);
  (* use_dsp = "yes" *) assign product = lhs * rhs;
endmodule

"""
        body = f"""
  (* keep = "true" *) wire [35:0] probe [0:{units - 1}];
  genvar i;
  generate for (i = 0; i < {units}; i = i + 1) begin : g_dsp
    (* dont_touch = "true", keep_hierarchy = "yes" *)
    calibration_dsp_cell u_cell(
      .lhs(stimulus[17:0] ^ i),
      .rhs(stimulus[35:18] + i),
      .product(probe[i])
    );
  end endgenerate
  integer k;
  reg reduction;
  always @* begin
    reduction = 1'b0;
    for (k = 0; k < {units}; k = k + 1) reduction = reduction ^ probe[k][0];
  end
  assign result = reduction;
"""
    elif resource == "bram":
        declarations = """(* keep_hierarchy = "yes" *)
module calibration_bram_cell(
  input wire clk,
  input wire [9:0] address,
  input wire [31:0] write_data,
  output reg [31:0] read_data
);
  (* ram_style = "block" *) reg [31:0] memory [0:1023];
  always @(posedge clk) begin
    memory[address] <= write_data;
    read_data <= memory[address];
  end
endmodule

"""
        body = f"""
  (* keep = "true" *) wire [31:0] probe [0:{units - 1}];
  genvar i;
  generate for (i = 0; i < {units}; i = i + 1) begin : g_bram
    (* dont_touch = "true", keep_hierarchy = "yes" *)
    calibration_bram_cell u_cell(
      .clk(clk),
      .address(stimulus[9:0]),
      .write_data(stimulus[31:0] ^ i),
      .read_data(probe[i])
    );
  end endgenerate
  reg reduction;
  integer k;
  always @* begin
    reduction = 1'b0;
    for (k = 0; k < {units}; k = k + 1)
      reduction = reduction ^ probe[k][0];
  end
  assign result = reduction;
"""
    else:
        raise ValidationError(f"capacity resource {resource!r} is not supported")
    return f"""{declarations}(* keep_hierarchy = "yes" *)
module calibration_capacity_probe(
  input wire clk,
  input wire [63:0] stimulus,
  output wire result
);
{body}endmodule

module calibration_top(
  input wire clk,
  input wire [63:0] stimulus,
  output wire result
);
  calibration_capacity_probe u_probe(.clk(clk), .stimulus(stimulus), .result(result));
endmodule
"""


def _link_rtl(payload_bits: int, parallel_flows: int) -> str:
    flow_wires = []
    flow_instances = []
    for flow in range(parallel_flows):
        flow_wires.append(
            f"  (* keep = \"true\" *) wire [{payload_bits - 1}:0] payload_f{flow};\n"
            f"  wire result_f{flow};"
        )
        flow_instances.append(
            f"  (* dont_touch = \"true\", keep_hierarchy = \"yes\" *)\n"
            f"  calibration_source u_source_f{flow}(\n"
            f"    .clk(clk), .stimulus(stimulus ^ {payload_bits}'d{flow}),\n"
            f"    .payload(payload_f{flow})\n"
            f"  );\n"
            f"  (* dont_touch = \"true\", keep_hierarchy = \"yes\" *)\n"
            f"  calibration_sink u_sink_f{flow}(\n"
            f"    .clk(clk), .payload(payload_f{flow}), .result(result_f{flow})\n"
            f"  );"
        )
    result_vector = ", ".join(
        f"result_f{flow}" for flow in reversed(range(parallel_flows))
    )
    return f"""(* keep_hierarchy = "yes" *)
module calibration_source #(
  parameter WIDTH = {payload_bits}
) (
  input wire clk,
  input wire [WIDTH-1:0] stimulus,
  output reg [WIDTH-1:0] payload
);
  always @(posedge clk) payload <= stimulus;
endmodule

(* keep_hierarchy = "yes" *)
module calibration_sink #(
  parameter WIDTH = {payload_bits}
) (
  input wire clk,
  input wire [WIDTH-1:0] payload,
  output reg result
);
  always @(posedge clk) result <= ^payload;
endmodule

module calibration_top(
  input wire clk,
  input wire [{payload_bits - 1}:0] stimulus,
  output wire result
);
{chr(10).join(flow_wires)}
{chr(10).join(flow_instances)}
  assign result = ^{{{result_vector}}};
endmodule
"""


def _link_instance_names(parallel_flows: int) -> tuple[list[str], list[str]]:
    return (
        [f"u_source_f{flow}" for flow in range(parallel_flows)],
        [f"u_sink_f{flow}" for flow in range(parallel_flows)],
    )


def validate_calibration_campaign(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "campaign")
    _reject_unknown(
        root, {"schema", "dataset", "configurations", "cases"}, "campaign"
    )
    if root.get("schema") != CAMPAIGN_SCHEMA:
        raise ValidationError(f"campaign.schema: expected {CAMPAIGN_SCHEMA!r}")
    dataset = dict(_mapping(root.get("dataset"), "campaign.dataset"))
    _reject_unknown(
        dataset,
        {
            "id",
            "role",
            "reference_alias",
            "source_class",
            "authorization_id",
            "publication_scope",
        },
        "campaign.dataset",
    )
    role = _string(dataset.get("role"), "campaign.dataset.role")
    if role not in {"fit", "holdout"}:
        raise ValidationError("campaign.dataset.role: expected fit or holdout")
    for field in (
        "id",
        "reference_alias",
        "source_class",
        "authorization_id",
        "publication_scope",
    ):
        _string(dataset.get(field), f"campaign.dataset.{field}")
    if dataset["source_class"] not in _SOURCE_CLASSES:
        raise ValidationError(
            "campaign.dataset.source_class: expected authorized_reference_flow "
            "or synthetic_fixture"
        )
    if dataset["publication_scope"] not in _PUBLICATION_SCOPES:
        raise ValidationError(
            "campaign.dataset.publication_scope: expected internal, aggregate_only, "
            "or public"
        )

    configurations = []
    for index, raw in enumerate(
        _array(root.get("configurations"), "campaign.configurations", nonempty=True)
    ):
        item = _mapping(raw, f"campaign.configurations[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "topology_file",
                "targets",
                "routes",
                "utilization_limits_percent",
            },
            f"campaign.configurations[{index}]",
        )
        limits = _mapping(
            item.get("utilization_limits_percent"),
            f"campaign.configurations[{index}].utilization_limits_percent",
        )
        _reject_unknown(
            limits,
            {"lut", "ff", "bram", "dsp"},
            f"campaign.configurations[{index}].utilization_limits_percent",
        )
        normalized_limits = {
            resource: _percentage(
                limits.get(resource),
                f"campaign.configurations[{index}].utilization_limits_percent.{resource}",
            )
            for resource in ("lut", "ff", "bram", "dsp")
        }
        targets = _mapping(item.get("targets"), f"campaign.configurations[{index}].targets")
        if not targets:
            raise ValidationError(f"campaign.configurations[{index}].targets: expected mappings")
        normalized_targets = {
            _string(logical, "logical FPGA ID"): _string(target, "reference target")
            for logical, target in targets.items()
        }
        routes = []
        for route_index, raw_route in enumerate(
            _array(item.get("routes"), f"campaign.configurations[{index}].routes", nonempty=True)
        ):
            route = _mapping(raw_route, "campaign route")
            _reject_unknown(
                route,
                {"id", "path", "control"},
                f"campaign.configurations[{index}].routes[{route_index}]",
            )
            control = _string(route.get("control"), "campaign route control")
            if control != "topology_unique_path":
                raise ValidationError(
                    "campaign route control: only topology_unique_path is supported"
                )
            path = [
                _string(fpga, "campaign route path")
                for fpga in _array(route.get("path"), "campaign route path", nonempty=True)
            ]
            if len(path) < 2 or any(fpga not in normalized_targets for fpga in path):
                raise ValidationError("campaign route must cover at least two declared FPGA IDs")
            if len(path) != len(set(path)):
                raise ValidationError("campaign route path must not repeat an FPGA")
            routes.append(
                {
                    "id": _identifier(route.get("id"), "campaign route id"),
                    "path": path,
                    "control": control,
                }
            )
        _unique(routes, "campaign routes")
        configurations.append(
            {
                "id": _identifier(
                    item.get("id"), f"campaign.configurations[{index}].id"
                ),
                "topology_file": _string(
                    item.get("topology_file"),
                    f"campaign.configurations[{index}].topology_file",
                ),
                "targets": normalized_targets,
                "routes": routes,
                "utilization_limits_percent": normalized_limits,
            }
        )
    _unique(configurations, "campaign configurations")
    configuration_by_id = {item["id"]: item for item in configurations}

    cases = []
    for index, raw in enumerate(_array(root.get("cases"), "campaign.cases", nonempty=True)):
        item = _mapping(raw, f"campaign.cases[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "kind",
                "configuration",
                "resource",
                "units",
                "target_fpga",
                "route",
                "payload_bits",
                "parallel_flows",
            },
            f"campaign.cases[{index}]",
        )
        case_id = _identifier(item.get("id"), f"campaign.cases[{index}].id")
        kind = _string(item.get("kind"), f"campaign.cases[{index}].kind")
        if kind not in _CASE_KINDS:
            raise ValidationError(f"campaign.cases[{index}].kind: unsupported case")
        configuration_id = _string(
            item.get("configuration"), f"campaign.cases[{index}].configuration"
        )
        if configuration_id not in configuration_by_id:
            raise ValidationError(f"campaign.cases[{index}]: unknown configuration")
        configuration = configuration_by_id[configuration_id]
        if kind == "capacity_boundary":
            resource = _string(item.get("resource"), f"campaign.cases[{index}].resource")
            if resource not in {"lut", "ff", "bram", "dsp"}:
                raise ValidationError(f"campaign.cases[{index}].resource: unsupported resource")
            target_fpga = _string(
                item.get("target_fpga"), f"campaign.cases[{index}].target_fpga"
            )
            if target_fpga not in configuration["targets"]:
                raise ValidationError(f"campaign.cases[{index}]: unknown target FPGA")
            cases.append(
                {
                    "id": case_id,
                    "kind": kind,
                    "configuration": configuration_id,
                    "resource": resource,
                    "units": _positive_integer(
                        item.get("units"), f"campaign.cases[{index}].units"
                    ),
                    "target_fpga": target_fpga,
                }
            )
        else:
            route_id = _string(item.get("route"), f"campaign.cases[{index}].route")
            route = next(
                (
                    candidate
                    for candidate in configuration["routes"]
                    if candidate["id"] == route_id
                ),
                None,
            )
            if route is None:
                raise ValidationError(f"campaign.cases[{index}]: unknown controlled route")
            if kind == "link_capacity_boundary" and len(route["path"]) != 2:
                raise ValidationError(
                    f"campaign.cases[{index}]: link-capacity fitting requires "
                    "a declared single-hop route"
                )
            cases.append(
                {
                    "id": case_id,
                    "kind": kind,
                    "configuration": configuration_id,
                    "route": route_id,
                    "route_path": route["path"],
                    "payload_bits": _positive_integer(
                        item.get("payload_bits"), f"campaign.cases[{index}].payload_bits"
                    ),
                    "parallel_flows": _positive_integer(
                        item.get("parallel_flows", 1), f"campaign.cases[{index}].parallel_flows"
                    ),
                }
            )
    _unique(cases, "campaign cases")
    return {
        "schema": CAMPAIGN_SCHEMA,
        "dataset": dataset,
        "configurations": configurations,
        "cases": cases,
    }


def plan_calibration_campaign(
    campaign_value: Mapping[str, Any], output_dir: Path
) -> Dict[str, Any]:
    campaign = validate_calibration_campaign(campaign_value)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValidationError(f"calibration campaign output is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    configurations = {item["id"]: item for item in campaign["configurations"]}
    manifest_cases = []
    for case in campaign["cases"]:
        case_dir = output_dir / "cases" / case["id"]
        case_dir.mkdir(parents=True)
        configuration = configurations[case["configuration"]]
        if case["kind"] == "capacity_boundary":
            rtl = _capacity_rtl(case["resource"], case["units"])
            constraints = (
                f"assign_inst {{u_probe}} "
                f"{{{configuration['targets'][case['target_fpga']]}}}\n"
            )
            controlled_route = None
            expected_assignment = {
                "u_probe": configuration["targets"][case["target_fpga"]]
            }
        else:
            rtl = _link_rtl(case["payload_bits"], case["parallel_flows"])
            source = case["route_path"][0]
            sink = case["route_path"][-1]
            source_instances, sink_instances = _link_instance_names(
                case["parallel_flows"]
            )
            constraints = "".join(
                f"assign_inst {{{instance}}} "
                f"{{{configuration['targets'][source]}}}\n"
                for instance in source_instances
            ) + "".join(
                f"assign_inst {{{instance}}} "
                f"{{{configuration['targets'][sink]}}}\n"
                for instance in sink_instances
            )
            controlled_route = case["route_path"]
            expected_assignment = {
                **{
                    instance: configuration["targets"][source]
                    for instance in source_instances
                },
                **{
                    instance: configuration["targets"][sink]
                    for instance in sink_instances
                },
            }
        (case_dir / "design.sv").write_text(rtl, encoding="utf-8")
        (case_dir / "filelist.f").write_text("design.sv\n", encoding="utf-8")
        (case_dir / "prepartition.cfg").write_text(constraints, encoding="utf-8")
        (case_dir / "run_ppro.tcl").write_text(
            _ppro_runner_tcl(
                case_id=case["id"],
                topology_file=configuration["topology_file"],
                utilization_limits_percent=configuration[
                    "utilization_limits_percent"
                ],
            ),
            encoding="utf-8",
        )
        shell_runner = case_dir / "run_ppro.sh"
        shell_runner.write_text(_ppro_runner_shell(), encoding="utf-8")
        shell_runner.chmod(0o755)
        manifest_cases.append(
            {
                **case,
                "top": "calibration_top",
                "source": str(Path("cases") / case["id"] / "design.sv"),
                "filelist": str(Path("cases") / case["id"] / "filelist.f"),
                "prepartition_config": str(
                    Path("cases") / case["id"] / "prepartition.cfg"
                ),
                "topology_file": configuration["topology_file"],
                "logical_targets": configuration["targets"],
                "utilization_limits_percent": configuration[
                    "utilization_limits_percent"
                ],
                "runner": {
                    "kind": "ppro_rtlpart_script_file_v1",
                    "script": str(Path("cases") / case["id"] / "run_ppro.tcl"),
                    "launcher": str(Path("cases") / case["id"] / "run_ppro.sh"),
                    "command": [
                        "bash",
                        str(Path("cases") / case["id"] / "run_ppro.sh"),
                    ],
                },
                "assignment_control": "fixed",
                "expected_assignment": expected_assignment,
                "route_control": (
                    "topology_unique_path"
                    if controlled_route is not None
                    else "not_applicable"
                ),
                "controlled_route": controlled_route,
                "result_contract": str(
                    Path("cases") / case["id"] / "result.json"
                ),
            }
        )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "dataset": campaign["dataset"],
        "cases": manifest_cases,
        "execution_policy": {
            "cold_start": True,
            "persistent_cache": False,
            "raw_reports_retained_in_repository": False,
            "infrastructure_failures_are_capacity_failures": False,
        },
    }
    write_json(output_dir / "campaign-manifest.json", manifest)
    return manifest


def collect_calibration_observations(
    manifest_value: Mapping[str, Any], result_root: Path
) -> Dict[str, Any]:
    manifest = _mapping(manifest_value, "campaign manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValidationError(f"campaign manifest.schema: expected {MANIFEST_SCHEMA!r}")
    capacity_boundaries = []
    link_capacity_boundaries = []
    link_characteristics = []
    delay_measurements = []
    excluded = []
    for raw_case in _array(manifest.get("cases"), "campaign manifest.cases", nonempty=True):
        case = _mapping(raw_case, "campaign manifest case")
        case_id = _identifier(case.get("id"), "campaign manifest case.id")
        expected_contract = Path("cases") / case_id / "result.json"
        if case.get("result_contract") != str(expected_contract):
            raise ValidationError(
                f"campaign manifest case {case_id!r}: invalid result contract"
            )
        result_path = result_root / expected_contract
        if not result_path.is_file():
            excluded.append({"id": case["id"], "reason": "missing_result"})
            continue
        result = validate_calibration_run_result(
            read_json(result_path), expected_case_id=case["id"]
        )
        status = result.get("status")
        if status not in _RUN_STATUSES:
            raise ValidationError(f"campaign result {case['id']!r}: unsupported status")
        if status in {"provider_fail", "license_fail", "transport_fail", "execution_fail"}:
            excluded.append({"id": case["id"], "reason": status})
            continue
        controls = _mapping(result.get("controls"), f"result {case['id']}.controls")
        if controls.get("assignment_applied") is not True:
            raise ValidationError(
                f"campaign result {case['id']!r}: fixed assignment was not applied"
            )
        observed_assignment = _mapping(
            controls.get("observed_assignment"),
            f"result {case['id']}.controls.observed_assignment",
        )
        if dict(observed_assignment) != case.get("expected_assignment"):
            raise ValidationError(
                f"campaign result {case['id']!r}: observed assignment does not match control"
            )
        metrics = _mapping(result.get("metrics"), f"result {case['id']}.metrics")
        if case["kind"] == "capacity_boundary":
            if status not in {"pass", "capacity_fail"}:
                raise ValidationError(f"campaign result {case['id']!r}: wrong failure class")
            demand = metrics.get("actual_resource_demand_per_fpga")
            demand = _positive_integer(
                demand, f"result {case['id']}.actual_resource_demand_per_fpga"
            )
            capacity_boundaries.append(
                {
                    "id": case["id"],
                    "configuration": case["configuration"],
                    "resource": case["resource"],
                    "demand_per_fpga": demand,
                    "utilization_limit": (
                        case["utilization_limits_percent"][case["resource"]] / 100.0
                    ),
                    "outcome": status,
                    "assignment_control": "fixed",
                }
            )
        elif case["kind"] == "link_capacity_boundary":
            if controls.get("route_applied") is not True:
                raise ValidationError(
                    f"campaign result {case['id']!r}: fixed route was not applied"
                )
            if controls.get("observed_route") != case["route_path"]:
                raise ValidationError(
                    f"campaign result {case['id']!r}: observed route does not match "
                    "the topology-unique path"
                )
            if status not in {"pass", "link_capacity_fail"}:
                raise ValidationError(f"campaign result {case['id']!r}: wrong failure class")
            link_capacity_boundaries.append(
                {
                    "id": case["id"],
                    "configuration": case["configuration"],
                    "hop_count": len(case["route_path"]) - 1,
                    "offered_bits_per_cycle": case["payload_bits"] * case["parallel_flows"],
                    "outcome": "pass" if status == "pass" else "capacity_fail",
                    "assignment_control": "fixed",
                    "route_control": "fixed",
                }
            )
            characteristic_fields = (
                "link_line_rate_mbps",
                "link_phy_width_bits",
                "link_channels_per_direction",
                "link_max_tdm_ratio_supported",
                "link_base_route_delay_ns",
            )
            if all(field in metrics for field in characteristic_fields):
                link_characteristics.append(
                    {
                        "id": f"{case['id']}-characteristic",
                        "configuration": case["configuration"],
                        "hop_count": 1,
                        "line_rate_mbps": metrics["link_line_rate_mbps"],
                        "phy_width_bits": metrics["link_phy_width_bits"],
                        "channels_per_direction": metrics[
                            "link_channels_per_direction"
                        ],
                        "max_tdm_ratio": metrics[
                            "link_max_tdm_ratio_supported"
                        ],
                        "base_route_delay_ns": metrics[
                            "link_base_route_delay_ns"
                        ],
                        "assignment_control": "fixed",
                        "route_control": "fixed",
                    }
                )
        else:
            if status != "pass" or controls.get("route_applied") is not True:
                raise ValidationError(
                    f"campaign result {case['id']!r}: delay case did not run its fixed route"
                )
            if controls.get("observed_route") != case["route_path"]:
                raise ValidationError(
                    f"campaign result {case['id']!r}: observed route does not match "
                    "the topology-unique path"
                )
            delay = metrics.get("sr0_worst_cross_fpga_delay_ns")
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay <= 0:
                raise ValidationError(
                    f"campaign result {case['id']!r}: missing positive cross-FPGA delay"
                )
            path_count = metrics.get("sr0_cross_fpga_path_count")
            _positive_integer(path_count, f"result {case['id']}.sr0_cross_fpga_path_count")
            max_tdm_ratio = metrics.get("sr0_max_tdm_ratio")
            _positive_integer(
                max_tdm_ratio, f"result {case['id']}.sr0_max_tdm_ratio"
            )
            delay_measurements.append(
                {
                    "id": case["id"],
                    "configuration": case["configuration"],
                    "hop_count": len(case["route_path"]) - 1,
                    "payload_bits": case["payload_bits"],
                    "max_tdm_ratio": max_tdm_ratio,
                    "contention_units": max(0, case["parallel_flows"] - 1),
                    "observed_delay_ns": float(delay),
                    "assignment_control": "fixed",
                    "route_control": "fixed",
                }
            )
    observations = {
        "schema": OBSERVATIONS_SCHEMA,
        "dataset": dict(manifest["dataset"]),
        "capacity_boundaries": capacity_boundaries,
        "link_capacity_boundaries": link_capacity_boundaries,
        "link_characteristics": link_characteristics,
        "link_delay_measurements": delay_measurements,
    }
    normalized = validate_calibration_observations(observations)
    normalized["collection"] = {
        "planned_cases": len(manifest["cases"]),
        "included_cases": (
            len(capacity_boundaries) + len(link_capacity_boundaries) + len(delay_measurements)
        ),
        "excluded_cases": excluded,
    }
    return normalized


def plan_calibration_campaign_files(spec_path: Path, output_dir: Path) -> Dict[str, Any]:
    manifest = plan_calibration_campaign(read_json(spec_path), output_dir)
    return {
        "status": "pass",
        "cases": len(manifest["cases"]),
        "manifest": str(output_dir / "campaign-manifest.json"),
    }


def collect_calibration_observation_files(
    manifest_path: Path, result_root: Path, output_path: Path
) -> Dict[str, Any]:
    observations = collect_calibration_observations(read_json(manifest_path), result_root)
    write_json(output_path, observations)
    return {
        "status": "pass",
        "included_cases": observations["collection"]["included_cases"],
        "excluded_cases": len(observations["collection"]["excluded_cases"]),
        "output": str(output_path),
    }
