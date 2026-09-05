"""Board-integrated Vivado implementation for one completed multi-FPGA flow."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Sequence

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .multi_fpga_bsp_flow import validate_multi_fpga_bsp_flow_report
from .multi_fpga_flow import validate_multi_fpga_flow_report
from .multi_fpga_physical_flow import validate_multi_fpga_physical_report
from .platform import Platform
from .serial_phy_provider import validate_serial_phy_provider
from .vivado_backend import vivado_runtime_xdc
from .vivado_netlist import emit_vivado_mapped_verilog


LEGACY_VIVADO_BOARD_FLOW_SCHEMA = "emuflow.vivado-board-flow/v1"
VIVADO_BOARD_FLOW_SCHEMA_V2 = "emuflow.vivado-board-flow/v2"
VIVADO_BOARD_FLOW_SCHEMA = "emuflow.vivado-board-flow/v3"

_VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS_V2 = (
    "congestion.rpt",
    "slr_utilization.rpt",
    "slr_crossing.rpt",
)
_VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS = (
    "congestion.rpt",
    "congestion.csv",
    "slr_utilization.rpt",
    "slr_crossing.rpt",
)
_VIVADO_BOARD_ARTIFACTS = (
    "synthesized.dcp",
    "placed.dcp",
    "routed.dcp",
    "route_status.rpt",
    "drc.rpt",
    "timing_summary.rpt",
    "utilization.rpt",
    *_VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS,
    "board_metrics.tsv",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_bundle_path(root: Path, raw_path: Any, context: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValidationError(f"{context} path is invalid")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"{context} path is unsafe")
    root = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError(f"{context} path uses a symlink")
    path = current.resolve()
    if path == root or root not in path.parents or not path.is_file():
        raise ValidationError(f"{context} artifact is missing")
    return path


def _sv_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return f"n_{name}" if not name or name[0].isdigit() else name


def _identifier(value: str) -> str:
    return f"\\{value} "


def _clock_ports(binding_id: str) -> tuple[str, str]:
    stem = f"refclk_{_sv_name(binding_id)}"
    return f"{stem}_p", f"{stem}_n"


def _reset_port(binding_id: str) -> str:
    return f"board_reset_{_sv_name(binding_id)}"


def _port_declaration(port: Mapping[str, Any]) -> str:
    direction = port.get("direction")
    width = port.get("width")
    if direction not in {"input", "output", "inout"}:
        raise ValidationError(f"invalid board-top port direction: {direction!r}")
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise ValidationError("invalid board-top port width")
    dimension = "" if width == 1 else f"[{width - 1}:0] "
    return f"  {direction} wire {dimension}{_identifier(port['id'])}"


def build_vivado_board_top(
    ir: EmuIR,
    fpga_record: Mapping[str, Any],
) -> str:
    """Connect an already lowered DUT+transport partition to Phase 6C."""

    fpga = fpga_record.get("fpga")
    if not isinstance(fpga, str) or not fpga:
        raise ValidationError("Phase 6C FPGA record has no identity")
    ports = {item["id"]: item for item in ir.value["ports"]}
    required = {
        "fabric_clk": "input",
        "reset": "input",
        "links_ready": "input",
    }
    for name, direction in required.items():
        if ports.get(name, {}).get("direction") != direction:
            raise ValidationError(
                f"{fpga}: mapped partition lacks {direction} {name!r}"
            )

    connections = fpga_record.get("transport_connections")
    if not isinstance(connections, list):
        raise ValidationError(f"{fpga}: transport connection inventory is invalid")
    internal_ports = {"links_ready"}
    link_wires = []
    partition_link_map: Dict[str, str] = {}
    wrapper_link_map: Dict[str, str] = {}
    for index, connection in enumerate(connections):
        width = connection.get("width")
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise ValidationError(f"{fpga}: link {index} has invalid width")
        stem = f"board_link_{index}"
        tx_wire = f"{stem}_tx"
        rx_wire = f"{stem}_rx"
        link_wires.extend(
            (
                f"  wire [{width - 1}:0] {tx_wire};",
                f"  wire [{width - 1}:0] {rx_wire};",
            )
        )
        for role, direction, wire in (
            ("transport_tx_port", "output", tx_wire),
            ("transport_rx_port", "input", rx_wire),
        ):
            name = connection.get(role)
            if name is None:
                if role == "transport_tx_port":
                    link_wires.append(f"  assign {tx_wire} = {width}'b0;")
                continue
            if ports.get(name, {}).get("direction") != direction:
                raise ValidationError(
                    f"{fpga}: mapped transport port {name!r} disagrees"
                )
            if ports[name].get("width") != width:
                raise ValidationError(
                    f"{fpga}: mapped transport port {name!r} width disagrees"
                )
            internal_ports.add(name)
            partition_link_map[name] = wire
        for role, wire in (
            ("wrapper_tx_port", tx_wire),
            ("wrapper_rx_port", rx_wire),
        ):
            name = connection.get(role)
            if not isinstance(name, str) or not name:
                raise ValidationError(f"{fpga}: wrapper link port is invalid")
            if name in wrapper_link_map:
                raise ValidationError(f"{fpga}: wrapper link port is repeated")
            wrapper_link_map[name] = wire

    external_ports = [
        port for port in ir.value["ports"] if port["id"] not in internal_ports
    ]
    external_names = {port["id"] for port in external_ports}
    serial_ports: list[tuple[str, str]] = []
    for clock in fpga_record.get("board_services", {}).get(
        "reference_clocks", []
    ):
        for name in _clock_ports(clock["id"]):
            serial_ports.append((name, "input"))
    for reset in fpga_record.get("board_services", {}).get("resets", []):
        serial_ports.append((_reset_port(reset["id"]), "input"))
    for site in fpga_record.get("sites", []):
        for direction in ("tx", "rx"):
            endpoint = site.get(direction)
            if endpoint is None:
                continue
            io_direction = "output" if direction == "tx" else "input"
            for polarity in ("p", "n"):
                serial_ports.append((endpoint["ports"][polarity], io_direction))
    serial_names = [name for name, _direction in serial_ports]
    if (
        len(set(serial_names)) != len(serial_names)
        or external_names.intersection(serial_names)
        or "links_ready_debug" in external_names
        or "links_ready_debug" in serial_names
    ):
        raise ValidationError(f"{fpga}: board-top external port collision")

    declarations = [_port_declaration(port) for port in external_ports]
    declarations.extend(
        f"  {direction} wire {name}" for name, direction in serial_ports
    )
    declarations.append("  output wire links_ready_debug")
    top = f"emuflow_board_top_{_sv_name(fpga)}"
    lines = [
        "// Generated board-integrated DUT+transport+serial top.",
        f"module {top} (",
        ",\n".join(declarations),
        ");",
        "",
        "  wire board_links_ready;",
        *link_wires,
        "  assign links_ready_debug = board_links_ready;",
        "",
        f"  {_identifier(ir.value['design']['top'])} mapped_partition (",
    ]
    partition_connections = []
    for port in ir.value["ports"]:
        name = port["id"]
        expression = (
            "board_links_ready"
            if name == "links_ready"
            else partition_link_map.get(name, _identifier(name))
        )
        partition_connections.append(
            f"    .{_identifier(name)}({expression})"
        )
    lines.extend([",\n".join(partition_connections), "  );", ""])
    wrapper_module = fpga_record.get("module")
    if not isinstance(wrapper_module, str) or not wrapper_module:
        raise ValidationError(f"{fpga}: serial wrapper module is invalid")
    wrapper_connections = [
        "    .fabric_clk(fabric_clk)",
        "    .reset(reset)",
    ]
    wrapper_connections.extend(
        f"    .{name}({wire})"
        for name, wire in sorted(wrapper_link_map.items())
    )
    wrapper_connections.extend(
        f"    .{name}({name})" for name, _direction in serial_ports
    )
    wrapper_connections.append("    .links_ready(board_links_ready)")
    lines.extend(
        [
            f"  {wrapper_module} serial_wrapper (",
            ",\n".join(wrapper_connections),
            "  );",
            "",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)


def _tcl_quote(value: str) -> str:
    return "{" + value.replace("\\", "/").replace("}", "\\}") + "}"


def build_vivado_board_tcl(
    *,
    part: str,
    top: str,
    sources: Sequence[Path],
    ip_sources: Sequence[Path],
    xdc_sources: Sequence[Path],
    post_synth_tcl: Path,
    output_dir: Path,
    expected_mapped_cells: int,
    expected_channels: int,
    expected_commons: int,
    expected_channel_locs: Sequence[str],
    expected_common_locs: Sequence[str],
    place_directive: str,
    route_directive: str,
) -> str:
    if not sources or not part.startswith("xc"):
        raise ValidationError("Vivado board implementation inputs are invalid")
    source_list = " ".join(_tcl_quote(str(path)) for path in sources)
    ip_list = " ".join(_tcl_quote(str(path)) for path in ip_sources)
    xdc_list = " ".join(_tcl_quote(str(path)) for path in xdc_sources)
    channel_locs = " ".join(_tcl_quote(item) for item in expected_channel_locs)
    common_locs = " ".join(_tcl_quote(item) for item in expected_common_locs)
    lines = [
        "create_project -in_memory -part " + _tcl_quote(part) + " emuflow_board",
    ]
    if ip_sources:
        lines.extend(
            [
                "foreach ip [list " + ip_list + "] { read_ip $ip }",
                "generate_target all [get_ips]",
                "synth_ip [get_ips]",
            ]
        )
    lines.extend(
        [
            "foreach source [list " + source_list + "] { read_verilog -sv $source }",
            "foreach constraint [list " + xdc_list + "] { read_xdc $constraint }",
            "synth_design -mode out_of_context -flatten_hierarchy none -top "
            + _tcl_quote(top)
            + " -part "
            + _tcl_quote(part),
            "source " + _tcl_quote(str(post_synth_tcl)),
            "set black_boxes [get_cells -quiet -hier -filter {IS_BLACKBOX == 1}]",
            "if {[llength $black_boxes] != 0} {",
            "  error \"board design has black boxes: $black_boxes\"",
            "}",
            "set mapped [get_cells -quiet -hier -filter {EMUFLOW_MAPPED == yes}]",
            "if {[llength $mapped] != " + str(expected_mapped_cells) + "} {",
            "  error \"mapped cell coverage disagrees\"",
            "}",
            "set channels [get_cells -quiet -hier -filter {REF_NAME == GTYE4_CHANNEL}]",
            "set commons [get_cells -quiet -hier -filter {REF_NAME == GTYE4_COMMON}]",
            "if {[llength $channels] != " + str(expected_channels) + "} {",
            "  error \"GT channel count disagrees\"",
            "}",
            "if {[llength $commons] != " + str(expected_commons) + "} {",
            "  error \"GT common count disagrees\"",
            "}",
            "set actual_channel_locs [lsort [get_property LOC $channels]]",
            "set expected_channel_locs [lsort [list " + channel_locs + "]]",
            "set actual_common_locs [lsort [get_property LOC $commons]]",
            "set expected_common_locs [lsort [list " + common_locs + "]]",
            "if {$actual_channel_locs ne $expected_channel_locs} {",
            "  error \"GT channel LOC coverage disagrees\"",
            "}",
            "if {$actual_common_locs ne $expected_common_locs} {",
            "  error \"GT common LOC coverage disagrees\"",
            "}",
            "write_checkpoint -force "
            + _tcl_quote(str(output_dir / "synthesized.dcp")),
            "opt_design",
            "place_design -directive " + _tcl_quote(place_directive),
            "write_checkpoint -force " + _tcl_quote(str(output_dir / "placed.dcp")),
            "route_design -directive " + _tcl_quote(route_directive),
            "write_checkpoint -force " + _tcl_quote(str(output_dir / "routed.dcp")),
            "report_route_status -file "
            + _tcl_quote(str(output_dir / "route_status.rpt")),
            "report_drc -file " + _tcl_quote(str(output_dir / "drc.rpt")),
            "report_timing_summary -file "
            + _tcl_quote(str(output_dir / "timing_summary.rpt")),
            "report_utilization -hierarchical -file "
            + _tcl_quote(str(output_dir / "utilization.rpt")),
            "report_design_analysis -congestion -min_congestion_level 3 -file "
            + _tcl_quote(str(output_dir / "congestion.rpt"))
            + " -csv "
            + _tcl_quote(str(output_dir / "congestion.csv")),
            "report_utilization -slr -file "
            + _tcl_quote(str(output_dir / "slr_utilization.rpt")),
            "set slrs [get_slrs -quiet]",
            "set slr_count [llength $slrs]",
            "set slr_crossing_status measured",
            "if {$slr_count > 1} {",
            "  report_slr_crossing -file "
            + _tcl_quote(str(output_dir / "slr_crossing.rpt")),
            "} else {",
            "  set slr_crossing_status single-slr-not-applicable",
            "  set slr_report [open "
            + _tcl_quote(str(output_dir / "slr_crossing.rpt"))
            + " w]",
            "  puts $slr_report \"EMUFLOW_SLR_CROSSING\\tstatus\\t$slr_crossing_status\"",
            "  puts $slr_report \"EMUFLOW_SLR_CROSSING\\tslr_count\\t$slr_count\"",
            "  close $slr_report",
            "}",
            "set unrouted [get_nets -quiet -filter {ROUTE_STATUS == UNROUTED}]",
            "if {[llength $unrouted] != 0} {",
            "  error \"board design has [llength $unrouted] unrouted nets\"",
            "}",
            "set drc_errors 0",
            "set drc_warnings 0",
            "foreach item [get_drc_violations -quiet] {",
            "  set severity [get_property SEVERITY $item]",
            "  if {$severity eq \"Error\" || "
            "$severity eq \"Critical Warning\"} {",
            "    incr drc_errors",
            "  } else {",
            "    incr drc_warnings",
            "  }",
            "}",
            "set timing_paths [get_timing_paths -quiet -max_paths 1 -nworst 1]",
            "set wns NA",
            "set critical_path NA",
            "if {[llength $timing_paths] != 0} {",
            "  set wns [get_property SLACK [lindex $timing_paths 0]]",
            "  set critical_path [get_property DATAPATH_DELAY "
            "[lindex $timing_paths 0]]",
            "}",
            "set metrics [open "
            + _tcl_quote(str(output_dir / "board_metrics.tsv"))
            + " w]",
            "puts $metrics \"metric\\tvalue\"",
            "puts $metrics \"vivado_version\\t[version -short]\"",
            "puts $metrics \"part\\t" + part + "\"",
            "puts $metrics \"mapped_cells\\t[llength $mapped]\"",
            "puts $metrics \"black_boxes\\t[llength $black_boxes]\"",
            "puts $metrics \"channel_primitives\\t[llength $channels]\"",
            "puts $metrics \"common_primitives\\t[llength $commons]\"",
            "puts $metrics \"unrouted_nets\\t[llength $unrouted]\"",
            "puts $metrics \"drc_errors\\t$drc_errors\"",
            "puts $metrics \"drc_warnings\\t$drc_warnings\"",
            "puts $metrics \"wns_ns\\t$wns\"",
            "puts $metrics \"critical_path_ns\\t$critical_path\"",
            "puts $metrics \"slr_count\\t$slr_count\"",
            "puts $metrics \"slr_crossing_status\\t$slr_crossing_status\"",
            "puts $metrics \"channel_locs\\t$actual_channel_locs\"",
            "puts $metrics \"common_locs\\t$actual_common_locs\"",
            "close $metrics",
            "puts \"EMUFLOW_BOARD_IMPL status=pass top=" + top + "\"",
            "exit 0",
            "",
        ]
    )
    return "\n".join(lines)


def _read_metrics(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise ValidationError("Vivado board metrics are missing")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "metric\tvalue":
        raise ValidationError("Vivado board metrics header is invalid")
    result: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, separator, value = line.partition("\t")
        if not separator or not key or key in result:
            raise ValidationError("Vivado board metrics row is invalid")
        result[key] = value
    return result


def _integer(metrics: Mapping[str, str], key: str) -> int:
    try:
        value = int(metrics[key])
    except (KeyError, ValueError) as error:
        raise ValidationError(f"Vivado board metric {key!r} is invalid") from error
    if value < 0:
        raise ValidationError(f"Vivado board metric {key!r} is negative")
    return value


def _read_drc_rule_summary(path: Path) -> Dict[str, Dict[str, Any]]:
    rules: Dict[str, Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 6:
            continue
        _empty, rule, severity, description, checks, _empty_tail = fields
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_-]*-[0-9]+", rule) is None
            or severity not in {"Warning", "Critical Warning", "Error"}
            or not checks.isdigit()
        ):
            continue
        rules[rule] = {
            "severity": severity,
            "description": description,
            "checks": int(checks),
        }
    return dict(sorted(rules.items()))


def validate_vivado_board_flow_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    schema = report.get("schema")
    if schema not in {
        LEGACY_VIVADO_BOARD_FLOW_SCHEMA,
        VIVADO_BOARD_FLOW_SCHEMA_V2,
        VIVADO_BOARD_FLOW_SCHEMA,
    }:
        raise ValidationError("Vivado board-flow report schema is invalid")
    if report.get("status") != "pass":
        raise ValidationError("Vivado board-flow report did not pass")
    if (
        report.get("qualification")
        != "vivado_ooc_board_integrated_place_route"
        or report.get("source_physical_backend") not in {"open", "vivado"}
        or report.get("tool", {}).get("name") != "vivado"
    ):
        raise ValidationError("Vivado board-flow qualification is invalid")
    source_bindings = report.get("source_bindings")
    if not isinstance(source_bindings, dict) or any(
        re.fullmatch(r"[0-9a-f]{64}", source_bindings.get(name, "")) is None
        for name in (
            "flow_report_sha256",
            "bsp_report_sha256",
            "provider_manifest_sha256",
        )
    ):
        raise ValidationError("Vivado board-flow source binding is invalid")
    records = report.get("fpgas")
    if not isinstance(records, list) or not records:
        raise ValidationError("Vivado board-flow FPGA inventory is empty")
    fpga_ids = [item.get("fpga") for item in records]
    if any(not isinstance(fpga, str) or not fpga for fpga in fpga_ids) or len(
        set(fpga_ids)
    ) != len(fpga_ids):
        raise ValidationError("Vivado board-flow FPGA identities are invalid")
    if any(
        item.get("status") != "pass"
        or item.get("closure", {}).get("unrouted_nets") != 0
        or not isinstance(item.get("closure", {}).get("drc_errors"), int)
        or item.get("closure", {}).get("drc_errors", -1) < 0
        or item.get("validation", {}).get("black_boxes") != 0
        for item in records
    ):
        raise ValidationError("Vivado board-flow closure is incomplete")
    physical_evidence_fpgas = 0
    multi_slr_fpgas = 0
    if schema in {VIVADO_BOARD_FLOW_SCHEMA_V2, VIVADO_BOARD_FLOW_SCHEMA}:
        expected_evidence = {
            "congestion": "congestion.rpt",
            "slr_crossing": "slr_crossing.rpt",
            "slr_utilization": "slr_utilization.rpt",
        }
        expected_artifacts = _VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS_V2
        if schema == VIVADO_BOARD_FLOW_SCHEMA:
            expected_evidence["congestion_csv"] = "congestion.csv"
            expected_artifacts = _VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS
        for item in records:
            evidence = item.get("physical_evidence")
            artifacts = item.get("artifacts")
            if (
                not isinstance(evidence, dict)
                or evidence.get("scope")
                != "authoritative-vivado-post-route-reports"
                or not isinstance(artifacts, dict)
            ):
                raise ValidationError(
                    "Vivado board-flow physical evidence is incomplete"
                )
            slr_count = evidence.get("slr_count")
            status = evidence.get("slr_crossing_status")
            if (
                isinstance(slr_count, bool)
                or not isinstance(slr_count, int)
                or slr_count < 0
                or status
                != (
                    "measured"
                    if slr_count > 1
                    else "single-slr-not-applicable"
                )
            ):
                raise ValidationError(
                    "Vivado board-flow SLR evidence is inconsistent"
                )
            evidence_artifacts = evidence.get("artifacts")
            if evidence_artifacts != expected_evidence:
                raise ValidationError(
                    "Vivado board-flow physical evidence inventory is invalid"
                )
            for name in expected_artifacts:
                artifact = artifacts.get(name)
                if (
                    not isinstance(artifact, dict)
                    or re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", ""))
                    is None
                    or isinstance(artifact.get("bytes"), bool)
                    or not isinstance(artifact.get("bytes"), int)
                    or artifact["bytes"] <= 0
                ):
                    raise ValidationError(
                        "Vivado board-flow physical artifact seal is invalid"
                    )
            physical_evidence_fpgas += 1
            multi_slr_fpgas += int(slr_count > 1)
    release = report.get("release")
    if (
        not isinstance(release, dict)
        or release.get("hardware_release_authorized") is not False
        or release.get("bitstreams_generated") != 0
    ):
        raise ValidationError("Vivado board-flow release boundary is invalid")
    return {
        "status": "pass",
        "design": report.get("design"),
        "platform": report.get("platform"),
        "fpgas": len(records),
        "unrouted_nets": 0,
        "drc_errors": sum(item["closure"]["drc_errors"] for item in records),
        "physical_evidence_fpgas": physical_evidence_fpgas,
        "multi_slr_fpgas": multi_slr_fpgas,
        "hardware_release_authorized": False,
    }


def validate_vivado_board_flow_bundle(output_dir: Path) -> Dict[str, Any]:
    """Rehash a relocatable v2/v3 board-flow bundle without rerunning Vivado."""

    output_dir = Path(output_dir).resolve()
    report_path = output_dir / "vivado-board-flow-report.json"
    if report_path.is_symlink() or not report_path.is_file():
        raise ValidationError("Vivado board-flow bundle report is missing")
    report = read_json(report_path)
    validation = validate_vivado_board_flow_report(report)
    schema = report.get("schema")
    if schema not in {VIVADO_BOARD_FLOW_SCHEMA_V2, VIVADO_BOARD_FLOW_SCHEMA}:
        raise ValidationError(
            "Vivado board-flow bundle validation requires relocatable v2/v3"
        )
    verified_paths: set[Path] = set()
    physical_artifacts = (
        _VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS_V2
        if schema == VIVADO_BOARD_FLOW_SCHEMA_V2
        else _VIVADO_PHYSICAL_EVIDENCE_ARTIFACTS
    )
    expected_labels = {
        "synthesized.dcp",
        "placed.dcp",
        "routed.dcp",
        "route_status.rpt",
        "drc.rpt",
        "timing_summary.rpt",
        "utilization.rpt",
        *physical_artifacts,
        "board_metrics.tsv",
        "generated_tcl",
    }
    for fpga_record in report["fpgas"]:
        fpga = fpga_record["fpga"]
        artifacts = fpga_record.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != expected_labels:
            raise ValidationError(
                f"{fpga}: Vivado board-flow artifact inventory is invalid"
            )
        for label, artifact in artifacts.items():
            if not isinstance(artifact, dict):
                raise ValidationError(
                    f"{fpga}: Vivado board-flow artifact seal is invalid"
                )
            expected_path = (
                f"{fpga}.vivado.tcl"
                if label == "generated_tcl"
                else f"{fpga}/{label}"
            )
            if artifact.get("path") != expected_path:
                raise ValidationError(
                    f"{fpga}: Vivado board-flow artifact path differs"
                )
            path = _safe_bundle_path(
                output_dir,
                artifact["path"],
                f"{fpga} Vivado board-flow {label}",
            )
            if (
                path in verified_paths
                or _sha256(path) != artifact.get("sha256")
                or path.stat().st_size != artifact.get("bytes")
            ):
                raise ValidationError(
                    f"{fpga}: Vivado board-flow artifact hash differs"
                )
            verified_paths.add(path)
        log = fpga_record.get("log")
        expected_log = f"{fpga}/vivado-board-implementation.log"
        if not isinstance(log, dict) or log.get("path") != expected_log:
            raise ValidationError(f"{fpga}: Vivado board-flow log seal is invalid")
        log_path = _safe_bundle_path(
            output_dir, log["path"], f"{fpga} Vivado board-flow log"
        )
        if log_path in verified_paths or _sha256(log_path) != log.get("sha256"):
            raise ValidationError(f"{fpga}: Vivado board-flow log hash differs")
        verified_paths.add(log_path)
    return {
        **validation,
        "artifacts_verified": len(verified_paths),
        "bundle_relocatable": True,
    }


def run_vivado_board_flow(
    *,
    flow_root: Path,
    bsp_root: Path,
    platform_path: Path,
    phy_provider_path: Path,
    vivado_executable: Path,
    output_dir: Path,
    place_directive: str = "Default",
    route_directive: str = "Default",
    write_bitstream: bool = False,
) -> Dict[str, Any]:
    """Place/route DUT, TDM transport, open PCS, and GT provider together."""

    if write_bitstream:
        raise ValidationError(
            "bitstream generation is blocked until board clock/reset and all "
            "top-level package-pin contracts are source-backed"
        )
    flow_root = flow_root.resolve()
    bsp_root = bsp_root.resolve()
    platform_path = platform_path.resolve()
    phy_provider_path = phy_provider_path.resolve()
    vivado_executable = vivado_executable.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and (
        not output_dir.is_dir() or any(output_dir.iterdir())
    ):
        raise EmuFlowError(f"Vivado board-flow output must be empty: {output_dir}")
    if not vivado_executable.is_file():
        raise ValidationError("Vivado board-flow executable is missing")

    bsp_report_path = bsp_root / "multi-fpga-bsp-flow-report.json"
    if not bsp_report_path.is_file():
        raise ValidationError("Vivado board-flow source report is missing")
    bsp_report = read_json(bsp_report_path)
    source_hash = bsp_report.get("source_flow_report_sha256")
    flow_candidates = (
        flow_root / "board-independent-flow-report.json",
        flow_root / "multi-fpga-flow-report.json",
    )
    flow_report_path = next(
        (
            path
            for path in flow_candidates
            if path.is_file() and _sha256(path) == source_hash
        ),
        None,
    )
    if flow_report_path is None:
        raise ValidationError(
            "Vivado board-flow BSP is not hash-bound to this source flow"
        )
    flow_report = read_json(flow_report_path)
    flow_validation = validate_multi_fpga_flow_report(flow_report)
    bsp_validation = validate_multi_fpga_bsp_flow_report(bsp_report)
    platform = Platform.load(platform_path)
    if (
        flow_validation["design"] != bsp_validation["design"]
        or flow_validation["platform"] != platform.name
        or bsp_validation["platform"] != platform.name
    ):
        raise ValidationError("Vivado board-flow source identities disagree")
    physical_summary = flow_report.get("physical")
    if not isinstance(physical_summary, dict):
        raise ValidationError(
            "Vivado board integration requires a completed physical flow"
        )
    if isinstance(physical_summary.get("fpgas"), list):
        physical = physical_summary
    else:
        artifact = flow_report.get("artifacts", {}).get(
            "physical_flow_report"
        )
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "sha256",
        }:
            raise ValidationError(
                "Vivado board integration physical artifact is missing"
            )
        physical_path = _safe_bundle_path(
            flow_root, artifact["path"], "physical flow report"
        )
        if _sha256(physical_path) != artifact["sha256"]:
            raise ValidationError(
                "Vivado board integration physical artifact hash disagrees"
            )
        physical = read_json(physical_path)
    source_physical_backend = physical.get("backend", {}).get("id")
    if source_physical_backend not in {"open", "vivado"}:
        raise ValidationError("Vivado board-flow source backend is invalid")
    validate_multi_fpga_physical_report(physical)
    runtime_path = flow_root / "runtime/runtime_contract.json"
    if not runtime_path.is_file():
        raise ValidationError("Vivado board-flow runtime contract is missing")
    runtime = read_json(runtime_path)

    provider_result = validate_serial_phy_provider(
        read_json(phy_provider_path), phy_provider_path, platform
    )
    provider = provider_result["normalized"]
    if provider.get("qualification") != "vendor_generated_hardware":
        raise ValidationError(
            "Vivado GT board integration requires a vendor-generated provider"
        )
    source_root = (phy_provider_path.parent / provider["source_root"]).resolve()
    provider_hdl = [
        (source_root / item["path"]).resolve()
        for item in provider["sources"]
        if item["language"] in {"systemverilog", "verilog"}
    ]
    vendor_ip = [
        (phy_provider_path.parent / item["path"]).resolve()
        for item in provider["vendor_products"]["xci"]
    ]
    if any(not path.is_file() for path in [*provider_hdl, *vendor_ip]):
        raise ValidationError("Vivado board-flow provider inventory is incomplete")

    phase6c_root = bsp_root / "phase6c"
    phase6c_report = read_json(phase6c_root / "phase6c_report.json")
    manifest = read_json(phase6c_root / "serial_wrapper_manifest.json")
    records = manifest.get("fpgas")
    if not isinstance(records, list):
        raise ValidationError("Vivado board-flow Phase 6C inventory is invalid")
    phase6c_by_fpga = {item["fpga"]: item for item in records}
    physical_by_fpga = {item["fpga"]: item for item in physical["fpgas"]}
    fpga_ids = {fpga.id for fpga in platform.fpgas}
    if set(phase6c_by_fpga) != fpga_ids or set(physical_by_fpga) != fpga_ids:
        raise ValidationError("Vivado board-flow does not cover every FPGA")

    shared_hdl = []
    for artifact in ("runtime_sync_rtl", "open_pcs_rtl"):
        names = phase6c_report.get("artifacts", {}).get(artifact, [])
        if not isinstance(names, list):
            raise ValidationError(f"Vivado board-flow {artifact} is invalid")
        shared_hdl.extend((phase6c_root / name).resolve() for name in names)
    if any(not path.is_file() for path in shared_hdl):
        raise ValidationError("Vivado board-flow Phase 6C source is missing")

    version = subprocess.run(
        [str(vivado_executable), "-version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode != 0:
        raise EmuFlowError("failed to query Vivado version")
    output_dir.mkdir(parents=True)
    implementations = []
    for fpga in sorted(platform.fpgas, key=lambda item: item.id):
        fpga_id = fpga.id
        record = phase6c_by_fpga[fpga_id]
        physical_record = physical_by_fpga[fpga_id]
        physical_root = flow_root / "physical" / fpga_id
        ir_path = physical_root / "placement.emuir.json"
        wrapper = phase6c_root / record["rtl"]
        gt_tcl_name = phase6c_report.get("artifacts", {}).get(
            "gt_site_tcl", {}
        ).get(fpga_id)
        if not isinstance(gt_tcl_name, str):
            raise ValidationError(f"{fpga_id}: trusted GT site Tcl is missing")
        gt_tcl = phase6c_root / gt_tcl_name
        required_paths = [ir_path, wrapper, gt_tcl]
        if any(not path.is_file() for path in required_paths):
            raise ValidationError(f"{fpga_id}: board implementation input is missing")
        ir = EmuIR.load(ir_path)
        fpga_out = output_dir / fpga_id
        fpga_out.mkdir(parents=True)
        source_mapped_verilog = physical_root / "partition.v"
        source_runtime_xdc = physical_root / "vivado/runtime.xdc"
        if source_mapped_verilog.is_file() and source_runtime_xdc.is_file():
            mapped_verilog = source_mapped_verilog
            runtime_xdc = source_runtime_xdc
            relowering = {"status": "not-required"}
        else:
            mapped_verilog = fpga_out / "partition.v"
            relowering = emit_vivado_mapped_verilog(
                ir_path,
                mapped_verilog,
                fpga_out / "mapped-verilog-report.json",
            )
            runtime_xdc = fpga_out / "runtime.xdc"
            runtime_xdc.write_text(
                vivado_runtime_xdc(ir_path, runtime), encoding="utf-8"
            )
        board_top = output_dir / f"{fpga_id}.board_top.sv"
        board_top.write_text(
            build_vivado_board_top(ir, record), encoding="utf-8"
        )
        top = f"emuflow_board_top_{_sv_name(fpga_id)}"
        board_xdc = phase6c_root / f"{fpga_id}.board_services.xdc"
        xdc_sources = [runtime_xdc]
        if board_xdc.is_file():
            xdc_sources.append(board_xdc)
        sources = [
            mapped_verilog,
            *provider_hdl,
            *shared_hdl,
            wrapper,
            board_top,
        ]
        channel_locs = [site["transceiver_site"] for site in record["sites"]]
        common_locs = [
            quad["common_site"] for quad in record["transceiver_quads"]
        ]
        expected_cells = (
            physical_record["original_cells"]
            + physical_record["transport_cells"]
        )
        script = output_dir / f"{fpga_id}.vivado.tcl"
        script.write_text(
            build_vivado_board_tcl(
                part=fpga.part,
                top=top,
                sources=sources,
                ip_sources=vendor_ip,
                xdc_sources=xdc_sources,
                post_synth_tcl=gt_tcl,
                output_dir=fpga_out,
                expected_mapped_cells=expected_cells,
                expected_channels=len(channel_locs),
                expected_commons=len(common_locs),
                expected_channel_locs=channel_locs,
                expected_common_locs=common_locs,
                place_directive=place_directive,
                route_directive=route_directive,
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                str(vivado_executable),
                "-mode",
                "batch",
                "-nojournal",
                "-nolog",
                "-source",
                str(script),
            ],
            cwd=fpga_out,
            check=False,
            capture_output=True,
            text=True,
        )
        log = fpga_out / "vivado-board-implementation.log"
        log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            detail = (completed.stdout + completed.stderr).splitlines()[-40:]
            raise EmuFlowError(
                f"{fpga_id}: Vivado board implementation failed\n"
                + "\n".join(detail)
            )
        metrics = _read_metrics(fpga_out / "board_metrics.tsv")
        if (
            metrics.get("part") != fpga.part
            or _integer(metrics, "mapped_cells") != expected_cells
            or _integer(metrics, "black_boxes") != 0
            or _integer(metrics, "channel_primitives") != len(channel_locs)
            or _integer(metrics, "common_primitives") != len(common_locs)
            or _integer(metrics, "unrouted_nets") != 0
        ):
            raise ValidationError(f"{fpga_id}: Vivado board metrics disagree")
        slr_count = _integer(metrics, "slr_count")
        slr_crossing_status = metrics.get("slr_crossing_status")
        if slr_crossing_status != (
            "measured" if slr_count > 1 else "single-slr-not-applicable"
        ):
            raise ValidationError(f"{fpga_id}: Vivado SLR metrics disagree")
        artifacts = {}
        for name in _VIVADO_BOARD_ARTIFACTS:
            path = fpga_out / name
            if not path.is_file() or path.stat().st_size == 0:
                raise ValidationError(f"{fpga_id}: Vivado artifact is missing")
            artifacts[name] = {
                "path": str(path.relative_to(output_dir)),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        artifacts["generated_tcl"] = {
            "path": str(script.relative_to(output_dir)),
            "sha256": _sha256(script),
            "bytes": script.stat().st_size,
        }
        implementations.append(
            {
                "fpga": fpga_id,
                "part": fpga.part,
                "status": "pass",
                "top": top,
                "source_physical_backend": source_physical_backend,
                "vivado_relowering": relowering,
                "cell_accounting": {
                    "mapped_partition_cells": expected_cells,
                    "gt_channels": len(channel_locs),
                    "gt_commons": len(common_locs),
                },
                "closure": {
                    "unrouted_nets": 0,
                    "drc_errors": _integer(metrics, "drc_errors"),
                    "drc_warnings": _integer(metrics, "drc_warnings"),
                    "drc_rule_summary": _read_drc_rule_summary(
                        fpga_out / "drc.rpt"
                    ),
                    "wns_ns": metrics.get("wns_ns"),
                    "critical_path_ns": metrics.get("critical_path_ns"),
                },
                "physical_evidence": {
                    "scope": "authoritative-vivado-post-route-reports",
                    "slr_count": slr_count,
                    "slr_crossing_status": slr_crossing_status,
                    "artifacts": {
                        "congestion": "congestion.rpt",
                        "congestion_csv": "congestion.csv",
                        "slr_crossing": "slr_crossing.rpt",
                        "slr_utilization": "slr_utilization.rpt",
                    },
                },
                "validation": {
                    "black_boxes": 0,
                    "channel_locs_exact": True,
                    "common_locs_exact": True,
                    "mapped_cell_coverage_exact": True,
                },
                "sources": [
                    {"path": str(path), "sha256": _sha256(path)}
                    for path in sources
                ],
                "constraints": [
                    {"path": str(path), "sha256": _sha256(path)}
                    for path in [*xdc_sources, gt_tcl]
                ],
                "artifacts": artifacts,
                "log": {
                    "path": str(log.relative_to(output_dir)),
                    "sha256": _sha256(log),
                },
            }
        )

    report = {
        "schema": VIVADO_BOARD_FLOW_SCHEMA,
        "status": "pass",
        "qualification": "vivado_ooc_board_integrated_place_route",
        "design": flow_validation["design"],
        "platform": platform.name,
        "source_physical_backend": source_physical_backend,
        "tool": {
            "name": "vivado",
            "executable": str(vivado_executable),
            "version": (version.stdout + version.stderr).strip(),
        },
        "source_bindings": {
            "flow_report_sha256": _sha256(flow_report_path),
            "bsp_report_sha256": _sha256(bsp_report_path),
            "provider_manifest_sha256": _sha256(phy_provider_path),
        },
        "fpgas": implementations,
        "release": {
            "hardware_release_authorized": False,
            "bitstreams_generated": 0,
            "blocked_on": [
                "source-backed fabric clock generation and phase alignment",
                "source-backed synchronous reset release",
                "complete package-pin binding for remaining top-level ports",
                "board-level runtime synchronization latency proof",
                "zero board-level DRC errors",
            ],
        },
    }
    report["summary"] = validate_vivado_board_flow_report(report)
    write_json(output_dir / "vivado-board-flow-report.json", report)
    return report
