"""Shared Vivado query, clock and scalar tool contracts."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .boundary_timing import BOUNDARY_IDENTITY_SCHEMA
from .boundary_identity_storage import read_boundary_identity_storage
from .placement import _vivado_mapped_name
from .ir import EmuIR


_ROOT = Path(__file__).resolve().parents[2]
_BOUNDARY_TIMING_SCRIPT = _ROOT / "scripts/vivado/export_boundary_timing.tcl"
_LOGIC_TIMING_SCRIPT = (
    _ROOT / "scripts/vivado/export_logic_segment_timing.tcl"
)
_BOUNDARY_QUERY_HEADER = (
    "endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\t"
    "boundary_cell_hex"
)


def _hex(value: str) -> str:
    return value.encode("utf-8").hex()


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
