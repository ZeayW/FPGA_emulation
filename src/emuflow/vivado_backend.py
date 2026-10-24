"""Vivado implementation/timing provider behind the common physical API."""

from __future__ import annotations

import csv
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .boundary_timing import (
    BOUNDARY_IDENTITY_SCHEMA,
    boundary_timing_header,
    iter_boundary_timing_records,
    validate_boundary_timing_database,
)
from .boundary_identity_storage import read_boundary_identity_storage
from .boundary_timing_storage import write_boundary_timing_storage
from .physical_backend import (
    PHYSICAL_PARTITION_RESULT_SCHEMA,
    validate_physical_partition_result,
)
from .placement import _vivado_mapped_name
from .sta import (
    import_sta_path_database_tsv_streaming,
    validate_sta_path_database,
    write_vivado_net_map,
)
from .ir import EmuIR
from .logic_segment_timing import import_vivado_logic_segment_timing
from .vivado_netlist import emit_vivado_mapped_verilog


VIVADO_PARTITION_REPORT_SCHEMA = "emuflow.vivado-partition-report/v1"
_ROOT = Path(__file__).resolve().parents[2]
_IMPLEMENT_SCRIPT = _ROOT / "scripts/vivado/implement_partition.tcl"
_TIMING_ANALYSIS_SCRIPT = _ROOT / "scripts/vivado/analyze_timing.tcl"
_TIMING_SCRIPT = _ROOT / "scripts/vivado/export_timing_path_database.tcl"
_BOUNDARY_TIMING_SCRIPT = _ROOT / "scripts/vivado/export_boundary_timing.tcl"
_LOGIC_TIMING_SCRIPT = (
    _ROOT / "scripts/vivado/export_logic_segment_timing.tcl"
)
_BOUNDARY_QUERY_HEADER = (
    "endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\t"
    "boundary_cell_hex"
)
_BOUNDARY_TIMING_HEADER = (
    "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex"
)


def _hex(value: str) -> str:
    return value.encode("utf-8").hex()


def _unhex(value: str, context: str) -> str:
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError(f"{context} is not valid UTF-8 hex") from error


def write_vivado_boundary_timing_query(
    ir_path: Path,
    identity_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    from .emuir_compact import is_compact_emuir
    from .indexed_physical_ir import IndexedPhysicalIR

    from .emuir_storage import is_emuir_sqlite

    indexed = is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path)
    ir = IndexedPhysicalIR(ir_path) if indexed else EmuIR.load(ir_path)
    identities = None
    try:
        identities = read_boundary_identity_storage(identity_path, allow_legacy=True)
        if identities.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
            raise ValidationError("Vivado boundary timing identity schema is invalid")
        net_index = ir.net_indices if indexed else {
            net["id"]: index for index, net in enumerate(ir.value["nets"])
        }
        count = 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as stream:
            stream.write(_BOUNDARY_QUERY_HEADER + "\n")
            for endpoint in identities.get("endpoints", []):
                count += 1
                _write_vivado_boundary_query_row(endpoint, net_index, stream)
        return {"status": "pass", "fpga": identities["fpga"], "endpoints": count, "output": str(output_path)}
    finally:
        if indexed:
            ir.close()
        close = getattr(identities, "close", None)
        if callable(close):
            close()


def _write_vivado_boundary_query_row(endpoint, net_index, stream):
    merged = endpoint["merged_ir"]
    logical_net = merged["logical_net"]
    if (
        endpoint["kind"] == "tx"
        and not merged["boundary_register_instances"]
        and logical_net not in net_index
    ):
        raise ValidationError(
            f"boundary endpoint {endpoint['id']!r} net is absent"
        )
    cells = merged["boundary_register_instances"]
    if endpoint["kind"] == "rx" and len(cells) != 1:
        raise ValidationError(
            f"RX endpoint {endpoint['id']!r} lacks one boundary register"
        )
    boundary_cell = _vivado_mapped_name(cells[0]) if cells else ""
    fields = (
        _hex(endpoint["id"]),
        endpoint["kind"],
        _hex(merged["external_port"]),
        str(merged["external_port_bit"]),
        _hex(
            f"__emuflow_net_{net_index[logical_net]}"
            if logical_net in net_index
            else ""
        ),
        _hex(boundary_cell),
    )
    stream.write("\t".join(fields) + "\n")

def import_vivado_boundary_timing(
    input_path: Path,
    identity_path: Path,
    output_path: Path,
    *,
    provider: str = "vivado-get-timing-paths-endpoint-v1",
    qualification: str = "routed-device-endpoint-exact",
    measurement_scope: str = "partition-boundary",
) -> Dict[str, Any]:
    identities = read_boundary_identity_storage(identity_path, allow_legacy=True)
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != _BOUNDARY_TIMING_HEADER:
        raise ValidationError("Vivado boundary timing TSV header is invalid")
    measurements = {}
    expected_kind = {
        item["id"]: item["kind"] for item in identities["endpoints"]
    }
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 5:
            raise ValidationError(
                f"Vivado boundary timing line {line_number} is malformed"
            )
        endpoint = _unhex(fields[0], f"line {line_number} endpoint")
        if endpoint in measurements or expected_kind.get(endpoint) != fields[1]:
            raise ValidationError(
                f"Vivado boundary timing line {line_number} identity disagrees"
            )
        try:
            delay = float(fields[2])
        except ValueError as error:
            raise ValidationError(
                f"Vivado boundary timing line {line_number} delay is invalid"
            ) from error
        measurements[endpoint] = {
            "delay_ns": delay,
            "start_object": _unhex(fields[3], f"line {line_number} start"),
            "end_object": _unhex(fields[4], f"line {line_number} end"),
        }
    header = boundary_timing_header(
        identities,
        provider=provider,
        qualification=qualification,
        measurement_scope=measurement_scope,
    )
    with write_boundary_timing_storage(
        output_path,
        header,
        iter_boundary_timing_records(
            identities,
            measurements,
            measurement_scope=measurement_scope,
        ),
    ) as database:
        validation = validate_boundary_timing_database(database, identities)
    close_identities = getattr(identities, "close", None)
    if callable(close_identities):
        close_identities()
    return {**validation, "output": str(output_path)}


def _read_vivado_cell_inventory(path: Path) -> Dict[str, str]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError(f"Vivado cell inventory is missing: {path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames != ["name", "ref_name"]:
            raise ValidationError(
                f"Vivado cell inventory has an invalid header: {path}"
            )
        result: Dict[str, str] = {}
        for line_number, row in enumerate(reader, start=2):
            name = row.get("name")
            ref_name = row.get("ref_name")
            if not name or not ref_name or len(row) != 2:
                raise ValidationError(
                    f"{path}:{line_number}: malformed cell inventory row"
                )
            if name in result:
                raise ValidationError(
                    f"{path}:{line_number}: duplicate cell {name!r}"
                )
            result[name] = ref_name
    return result


def validate_vivado_cell_coverage(
    ir: EmuIR,
    synthesized_inventory_path: Path,
    routed_inventory_path: Path,
) -> Dict[str, Any]:
    """Prove logical coverage without constraining legal Vivado transforms.

    EMUFLOW_MAPPED follows each emitted EmuIR instance through synthesis and
    implementation.  Vivado is free to add, absorb, or rename untagged
    provider-internal cells, particularly while lowering inferred RAMs and
    DSPs; those implementation cells are deliberately outside this identity
    comparison.
    """

    expected = {
        _vivado_mapped_name(instance["id"])
        for instance in ir.value["instances"]
    }
    if len(expected) != len(ir.value["instances"]):
        raise ValidationError("Vivado mapped cell names are not unique")
    synthesized = _read_vivado_cell_inventory(synthesized_inventory_path)
    routed = _read_vivado_cell_inventory(routed_inventory_path)
    for stage, actual in (("synthesized", synthesized), ("routed", routed)):
        missing = expected - set(actual)
        extra = set(actual) - expected
        if missing or extra:
            raise ValidationError(
                f"Vivado {stage} logical-cell coverage disagrees: "
                f"missing={len(missing)}, extra={len(extra)}"
            )
    changed_refs = sum(
        synthesized[name] != routed[name] for name in expected
    )
    return {
        "status": "pass",
        "provider": "emuflow-mapped-attribute-identity-v1",
        "logical_cells": len(expected),
        "synthesized_cells": len(synthesized),
        "routed_cells": len(routed),
        "reference_type_changes": changed_refs,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_vivado(executable: Optional[str]) -> str:
    candidate = executable or "vivado"
    resolved = shutil.which(candidate)
    if resolved is None:
        path = Path(candidate).expanduser()
        if path.is_file():
            resolved = str(path.resolve())
    if resolved is None:
        raise EmuFlowError(
            "Vivado executable was not found; pass --physical-vivado or "
            "select --physical-backend open"
        )
    return resolved


def vivado_runtime_xdc(
    ir_path: Path,
    runtime: Mapping[str, Any],
) -> str:
    from .emuir_compact import is_compact_emuir
    from .indexed_physical_ir import IndexedPhysicalIR

    from .emuir_storage import is_emuir_sqlite

    if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
        with IndexedPhysicalIR(ir_path) as ir:
            ports = {item["id"] for item in ir.value["ports"]}
    else:
        ports = {item["id"] for item in EmuIR.load(ir_path).value["ports"]}
    timing = runtime["timing_model"]
    dut_ports = timing["dut_clock_ports"]
    fabric_port = timing["fabric_clock_port"]
    if fabric_port not in ports:
        raise ValidationError(
            "Vivado physical partition lacks required clock ports: "
            + fabric_port
        )
    fabric_period = runtime["fabric_clock"]["period_ns"]
    cross_delay = timing["fabric_to_dut_max_delay_ns"]
    lines = ["# EmuFlow provider-neutral runtime timing contract."]
    lines.append(
        f"create_clock -name emuflow_fabric_clk -period "
        f"{fabric_period:.9f} [get_ports {{{fabric_port}}}]"
    )
    for index, dut_port in enumerate(dut_ports):
        if dut_port not in ports:
            continue
        dut_period = runtime["virtual_dut_clocks"][dut_port][
            "nominal_period_ns"
        ]
        clock_name = f"emuflow_dut_clk_{index}"
        lines.extend(
            [
                f"create_clock -name {clock_name} -period {dut_period:.9f} "
                f"[get_ports {{{dut_port}}}]",
                f"set_max_delay -datapath_only {cross_delay:.9f} "
                "-from [get_clocks {emuflow_fabric_clk}] "
                f"-to [get_clocks {{{clock_name}}}]",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def vivado_design_timing_xdc(
    ir_path: Path,
    clocks: Mapping[str, float],
) -> str:
    ir = EmuIR.load(ir_path)
    ports = {item["id"] for item in ir.value["ports"]}
    if not clocks:
        raise ValidationError("Vivado timing requires at least one clock")
    lines = ["# EmuFlow Vivado timing-provider clock contract."]
    for index, (clock, period) in enumerate(sorted(clocks.items())):
        if clock not in ports:
            raise ValidationError(
                f"Vivado timing clock port {clock!r} is absent from EmuIR"
            )
        if (
            isinstance(period, bool)
            or not isinstance(period, (int, float))
            or period <= 0
        ):
            raise ValidationError(
                f"Vivado timing period for {clock!r} must be positive"
            )
        lines.append(
            f"create_clock -name emuflow_clock_{index} "
            f"-period {float(period):.9f} [get_ports {{{clock}}}]"
        )
    lines.append("")
    return "\n".join(lines)


def _read_metrics(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise ValidationError(
            f"Vivado did not emit implementation metrics: {path}"
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "metric\tvalue":
        raise ValidationError("Vivado implementation metrics header is invalid")
    result: Dict[str, str] = {}
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or fields[0] in result:
            raise ValidationError(
                f"Vivado implementation metrics line {line_number} is invalid"
            )
        result[fields[0]] = fields[1]
    return result


def _integer(metrics: Mapping[str, str], name: str) -> int:
    try:
        value = int(metrics[name])
    except (KeyError, ValueError) as error:
        raise ValidationError(f"Vivado metric {name!r} is not an integer") from error
    if value < 0:
        raise ValidationError(f"Vivado metric {name!r} is negative")
    return value


def _number(metrics: Mapping[str, str], name: str) -> float:
    try:
        return float(metrics[name])
    except (KeyError, ValueError) as error:
        raise ValidationError(f"Vivado metric {name!r} is not numeric") from error


def _run_vivado(
    executable: str,
    script: Path,
    arguments: list[str],
    output_dir: Path,
    log_name: str,
) -> Dict[str, Any]:
    command = [
        executable,
        "-mode",
        "batch",
        "-nojournal",
        "-nolog",
        "-source",
        str(script),
        "-tclargs",
        *arguments,
    ]
    completed = subprocess.run(
        command,
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log_path = output_dir / log_name
    log_path.write_text(completed.stdout, encoding="utf-8")
    critical_warnings = completed.stdout.count("CRITICAL WARNING:")
    if completed.returncode != 0 or critical_warnings:
        reason = (
            f"exit code {completed.returncode}"
            if completed.returncode != 0
            else f"{critical_warnings} critical warning(s)"
        )
        raise EmuFlowError(
            f"Vivado script {script.name} failed with {reason}\n"
            + "\n".join(completed.stdout.splitlines()[-40:])
        )
    return {
        "status": "pass",
        "command": command,
        "log": str(log_path),
        "log_sha256": _sha256(log_path),
        "critical_warnings": 0,
    }


def run_vivado_timing_path_database(
    *,
    ir_path: Path,
    output_path: Path,
    clocks: Mapping[str, float],
    part: str,
    executable: Optional[str] = None,
    max_paths: int = 200000,
) -> Dict[str, Any]:
    """Use Vivado timing as a drop-in producer of the common STA PathDB."""
    if not part.startswith("xc"):
        raise ValidationError(
            f"Vivado timing requires a concrete Xilinx part, got {part!r}"
        )
    if max_paths <= 0:
        raise ValidationError("Vivado timing max paths must be positive")
    ir_path = ir_path.resolve()
    output_path = output_path.resolve()
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    vivado = _resolve_vivado(executable)
    ir = EmuIR.load(ir_path)
    mapped_verilog = output_dir / "vivado-timing-netlist.v"
    netlist_report = emit_vivado_mapped_verilog(
        ir_path,
        mapped_verilog,
        output_dir / "vivado-timing-netlist-report.json",
    )
    xdc_path = output_dir / "vivado-timing.xdc"
    xdc_path.write_text(
        vivado_design_timing_xdc(ir_path, clocks), encoding="utf-8"
    )
    analysis = _run_vivado(
        vivado,
        _TIMING_ANALYSIS_SCRIPT,
        [
            part,
            str(mapped_verilog),
            ir.value["design"]["top"],
            str(xdc_path),
            str(output_dir),
            str(len(ir.value["instances"])),
        ],
        output_dir,
        "vivado-timing-analysis.log",
    )
    metrics = _read_metrics(output_dir / "timing_metrics.tsv")
    if metrics.get("part") != part:
        raise ValidationError("Vivado timing analyzed a different part")
    if _integer(metrics, "mapped_cells") != len(ir.value["instances"]):
        raise ValidationError("Vivado timing mapped-cell coverage disagrees")
    net_map = output_dir / "vivado-net-map.tsv"
    net_map_report = write_vivado_net_map(ir_path, net_map)
    timing_tsv = output_dir / "vivado-timing-paths.tsv"
    export = _run_vivado(
        vivado,
        _TIMING_SCRIPT,
        [
            str(output_dir / "timing.dcp"),
            str(net_map),
            str(timing_tsv),
            str(max_paths),
        ],
        output_dir,
        "vivado-timing-path-export.log",
    )
    rows = [
        line
        for line in timing_tsv.read_text(encoding="utf-8").splitlines()[1:]
        if line
    ]
    if not rows:
        raise ValidationError(
            "Vivado timing produced no mapped register-to-register paths"
        )
    import_report = import_sta_path_database_tsv_streaming(
        timing_tsv,
        ir_path,
        output_path,
        provider="vivado-get-timing-path-database-v1",
    )
    validation = validate_sta_path_database(output_path, ir_path)
    report = {
        "status": "pass",
        "provider": "vivado-get-timing-path-database-v1",
        "mode": "vivado-post-synthesis",
        "tool": {"executable": vivado, "version": metrics["vivado_version"]},
        "part": part,
        "netlist": netlist_report,
        "analysis": analysis,
        "path_export": export,
        "net_map": net_map_report,
        "database": import_report,
        "validation": validation,
        "output": str(output_path),
    }
    write_json(output_dir / "vivado-timing-provider-report.json", report)
    return report


def run_vivado_partition_backend(
    *,
    fpga: str,
    part: str,
    ir_path: Path,
    mapped_verilog_path: Path,
    runtime: Mapping[str, Any],
    original_cells: int,
    transport_cells: int,
    output_dir: Path,
    boundary_identity_path: Optional[Path] = None,
    logic_identity_path: Optional[Path] = None,
    logic_query_path: Optional[Path] = None,
    executable: Optional[str] = None,
    max_timing_paths: int = 10000,
    place_directive: str = "Default",
    route_directive: str = "Default",
    mapped_report: Optional[Mapping[str, Any]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    local_identity_path: Optional[Path] = None,
    local_query_path: Optional[Path] = None,
    synthesized_checkpoint: Optional[Path] = None,
    synthesized_checkpoint_sha256: Optional[str] = None,
    continuation_source_report: Optional[Path] = None,
) -> Dict[str, Any]:
    from .emuir_storage import is_emuir_sqlite
    from .emuir_compact import is_compact_emuir

    if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
        from .vivado_physical import run_indexed_vivado_partition

        if mapped_report is None:
            raise ValidationError("indexed Vivado execution requires its mapped export report")
        return run_indexed_vivado_partition(
            fpga=fpga, part=part, ir_path=ir_path, mapped_verilog_path=mapped_verilog_path,
            mapped_report=mapped_report, runtime=runtime, original_cells=original_cells,
            transport_cells=transport_cells, output_dir=output_dir,
            boundary_identity_path=boundary_identity_path, logic_identity_path=logic_identity_path,
            logic_query_path=logic_query_path, local_identity_path=local_identity_path,
            local_query_path=local_query_path, virtual_io_contract=virtual_io_contract,
            executable=executable, place_directive=place_directive, route_directive=route_directive,
            synthesized_checkpoint=synthesized_checkpoint, synthesized_checkpoint_sha256=synthesized_checkpoint_sha256,
            continuation_source_report=continuation_source_report,
        )
    if any(value is not None for value in (synthesized_checkpoint,synthesized_checkpoint_sha256,continuation_source_report)):
        raise ValidationError("synthesized continuation requires the current indexed Vivado provider")
    if virtual_io_contract is not None or local_identity_path is not None or local_query_path is not None:
        raise ValidationError("current fixed-I/O/local timing requires compact EmuIR input")
    if not part.startswith("xc"):
        raise ValidationError(
            f"Vivado backend requires a concrete Xilinx part, got {part!r}"
        )
    if max_timing_paths <= 0:
        raise ValidationError("Vivado max timing paths must be positive")
    ir_path = ir_path.resolve()
    mapped_verilog_path = mapped_verilog_path.resolve()
    if boundary_identity_path is not None:
        boundary_identity_path = boundary_identity_path.resolve()
    if (logic_identity_path is None) != (logic_query_path is None):
        raise ValidationError(
            "Vivado logic timing identity and query must be provided together"
        )
    if logic_identity_path is not None and logic_query_path is not None:
        logic_identity_path = logic_identity_path.resolve()
        logic_query_path = logic_query_path.resolve()
    for name, path in (("EmuIR", ir_path), ("mapped Verilog", mapped_verilog_path)):
        if not path.is_file() or path.stat().st_size == 0:
            raise EmuFlowError(f"Vivado {name} input is missing: {path}")
    for name, path in (
        ("logic timing identity", logic_identity_path),
        ("logic timing query", logic_query_path),
    ):
        if path is not None and (not path.is_file() or path.stat().st_size == 0):
            raise EmuFlowError(f"Vivado {name} input is missing: {path}")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    vivado = _resolve_vivado(executable)
    ir = EmuIR.load(ir_path)
    top = ir.value["design"]["top"]
    expected_cells = original_cells + transport_cells
    if len(ir.value["instances"]) != expected_cells:
        raise ValidationError(
            f"Vivado input instance count for {fpga} disagrees with accounting"
        )

    xdc_path = output_dir / "runtime.xdc"
    xdc_path.write_text(vivado_runtime_xdc(ir_path, runtime), encoding="utf-8")
    implementation = _run_vivado(
        vivado,
        _IMPLEMENT_SCRIPT,
        [
            part,
            str(mapped_verilog_path),
            top,
            str(xdc_path),
            str(output_dir),
            str(expected_cells),
            str(runtime["virtual_dut_clock"]["nominal_period_ns"]),
            place_directive,
            route_directive,
        ],
        output_dir,
        "vivado-implementation.log",
    )
    metrics_path = output_dir / "implementation_metrics.tsv"
    metrics = _read_metrics(metrics_path)
    if metrics.get("part") != part:
        raise ValidationError("Vivado implemented a different part")
    if _integer(metrics, "mapped_cells") != expected_cells:
        raise ValidationError("Vivado mapped-cell coverage disagrees")
    cell_coverage = validate_vivado_cell_coverage(
        ir,
        output_dir / "mapped_cells.tsv",
        output_dir / "routed_mapped_cells.tsv",
    )
    expected_dsp = sum(
        item["type"] == "VTR_MULTIPLY" for item in ir.value["instances"]
    )
    expected_bram = sum(
        item["type"] in {"VTR_SP_RAM", "VTR_DP_RAM"}
        for item in ir.value["instances"]
    )
    expected_bram_bits = sum(
        int(item.get("parameters", {}).get("DEPTH", 0))
        * int(item.get("parameters", {}).get("DATA_WIDTH", 0))
        for item in ir.value["instances"]
        if item["type"] in {"VTR_SP_RAM", "VTR_DP_RAM"}
    )
    dsp48_cells = _integer(metrics, "dsp48_cells")
    ramb18_cells = _integer(metrics, "ramb18_cells")
    ramb36_cells = _integer(metrics, "ramb36_cells")
    if dsp48_cells < expected_dsp:
        raise ValidationError(
            f"Vivado inferred {dsp48_cells} DSP48 cells for "
            f"{expected_dsp} VTR multipliers"
        )
    realized_bram_bits = 18432 * ramb18_cells + 36864 * ramb36_cells
    if realized_bram_bits < expected_bram_bits:
        raise ValidationError(
            "Vivado inferred insufficient block-RAM capacity for "
            f"{expected_bram} VTR RAM macros ({expected_bram_bits} bits)"
        )

    net_map = output_dir / "vivado-net-map.tsv"
    net_map_report = write_vivado_net_map(ir_path, net_map)
    timing_tsv = output_dir / "timing-paths.tsv"
    timing_export = _run_vivado(
        vivado,
        _TIMING_SCRIPT,
        [
            str(output_dir / "routed.dcp"),
            str(net_map),
            str(timing_tsv),
            str(max_timing_paths),
        ],
        output_dir,
        "vivado-timing-export.log",
    )
    timing_database = output_dir / "timing-path-database.json"
    timing_rows = [
        line
        for line in timing_tsv.read_text(encoding="utf-8").splitlines()[1:]
        if line
    ]
    if timing_rows:
        timing_database_report = import_sta_path_database_tsv_streaming(
            timing_tsv,
            ir_path,
            timing_database,
            provider="vivado-get-timing-path-database-v1",
        )
        timing_database_validation = validate_sta_path_database(
            timing_database, ir_path
        )
    else:
        timing_database_report = {
            "status": "pass",
            "design": ir.value["design"]["name"],
            "paths": 0,
            "qualification": "no-register-to-register-paths",
        }
        timing_database_validation = dict(timing_database_report)

    boundary_timing_report = None
    if boundary_identity_path is not None:
        boundary_query = output_dir / "boundary-timing-query.tsv"
        query_report = write_vivado_boundary_timing_query(
            ir_path, boundary_identity_path, boundary_query
        )
        boundary_tsv = output_dir / "boundary-timing.tsv"
        boundary_export = _run_vivado(
            vivado,
            _BOUNDARY_TIMING_SCRIPT,
            [
                str(output_dir / "routed.dcp"),
                str(boundary_query),
                str(boundary_tsv),
            ],
            output_dir,
            "vivado-boundary-timing-export.log",
        )
        boundary_database = output_dir / "boundary-timing.json"
        boundary_import = import_vivado_boundary_timing(
            boundary_tsv, boundary_identity_path, boundary_database
        )
        boundary_timing_report = {
            "status": "pass",
            "query": query_report,
            "export": boundary_export,
            "import": boundary_import,
        }

    logic_timing_report = None
    if logic_identity_path is not None and logic_query_path is not None:
        logic_tsv = output_dir / "logic-segment-timing.tsv"
        logic_export = _run_vivado(
            vivado,
            _LOGIC_TIMING_SCRIPT,
            [
                str(output_dir / "routed.dcp"),
                str(logic_query_path),
                str(logic_tsv),
            ],
            output_dir,
            "vivado-logic-segment-timing-export.log",
        )
        logic_database = output_dir / "logic-segment-timing.json"
        logic_import = import_vivado_logic_segment_timing(
            logic_tsv, logic_identity_path, logic_database
        )
        logic_timing_report = {
            "status": "pass",
            "export": logic_export,
            "import": logic_import,
        }

    artifact_names = (
        "synthesized.dcp",
        "placed.dcp",
        "routed.dcp",
        "route_status.rpt",
        "drc.rpt",
        "timing_summary.rpt",
        "utilization.rpt",
        "implementation_metrics.tsv",
        "mapped_cells.tsv",
        "routed_mapped_cells.tsv",
        *(("timing-path-database.json",) if timing_rows else ()),
        *(("timing-path-database.json.paths.sqlite",) if timing_rows else ()),
        *(("boundary-timing.json",) if boundary_timing_report else ()),
        *(("logic-segment-timing.json",) if logic_timing_report else ()),
    )
    artifacts = {}
    for name in artifact_names:
        path = output_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValidationError(f"Vivado artifact is missing: {path}")
        artifacts[name] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }

    infrastructure_cells = _integer(metrics, "infrastructure_cells")
    physical_cells = _integer(metrics, "physical_cells")
    result = {
        "schema": PHYSICAL_PARTITION_RESULT_SCHEMA,
        "status": "pass",
        "identity": {"backend": "vivado", "fpga": fpga, "part": part},
        "cell_accounting": {
            "original_cells": original_cells,
            "transport_cells": transport_cells,
            "routed_cells": expected_cells,
            "physical_cells": physical_cells,
            "infrastructure_cells": infrastructure_cells,
            "optimization_cells": _integer(metrics, "optimization_cells"),
        },
        "closure": {
            "unrouted_nets": _integer(metrics, "unrouted_nets"),
            "drc_violations": _integer(metrics, "drc_violations"),
            "drc_warnings": _integer(metrics, "drc_warnings"),
        },
        "hard_resources": {
            "vtr_multiply_macros": expected_dsp,
            "vtr_ram_macros": expected_bram,
            "vtr_ram_bits": expected_bram_bits,
            "dsp48_cells": dsp48_cells,
            "ramb18_cells": ramb18_cells,
            "ramb36_cells": ramb36_cells,
        },
        "clocks": {
            "fabric_period_ns": _number(metrics, "fabric_period_ns"),
            "dut_period_ns": _number(metrics, "dut_period_ns"),
        },
        "timing": {
            "wns_ns": _number(metrics, "wns_ns"),
            "timing_met": _number(metrics, "wns_ns") >= 0,
            "critical_path_ns": _number(metrics, "critical_path_ns"),
            "dut_wns_ns": _number(metrics, "dut_wns_ns"),
            "fabric_wns_ns": _number(metrics, "fabric_wns_ns"),
            "fabric_to_dut_wns_ns": _number(
                metrics, "fabric_to_dut_wns_ns"
            ),
            "clock_domain_delays_ns": {
                "dut": _number(metrics, "dut_delay_ns"),
                "fabric": _number(metrics, "fabric_delay_ns"),
                "cross": _number(metrics, "fabric_to_dut_delay_ns"),
                "overall": _number(metrics, "critical_path_ns"),
            },
        },
        "artifacts": artifacts,
    }
    validation = validate_physical_partition_result(
        result,
        backend="vivado",
        fpga=fpga,
        part=part,
        original_cells=original_cells,
        transport_cells=transport_cells,
    )
    report = {
        "schema": VIVADO_PARTITION_REPORT_SCHEMA,
        "status": "pass",
        "provider": "vivado-implementation-and-timing-v1",
        "tool": {"executable": vivado, "version": metrics["vivado_version"]},
        "implementation": implementation,
        "timing_export": timing_export,
        "cell_coverage": cell_coverage,
        "net_map": net_map_report,
        "timing_path_database": timing_database_report,
        "timing_path_validation": timing_database_validation,
        **(
            {"boundary_timing": boundary_timing_report}
            if boundary_timing_report is not None
            else {}
        ),
        **(
            {"logic_segment_timing": logic_timing_report}
            if logic_timing_report is not None
            else {}
        ),
        "result": result,
        "validation": validation,
    }
    write_json(output_dir / "vivado-partition-report.json", report)
    return report
