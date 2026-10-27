"""Shared Vivado query, clock and scalar tool contracts."""

from __future__ import annotations

from emuflow.common.paths import REPOSITORY_ROOT

import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.ir.ir import EmuIR


_ROOT = REPOSITORY_ROOT
_BOUNDARY_TIMING_SCRIPT = _ROOT / "scripts/vivado/export_boundary_timing.tcl"
_LOGIC_TIMING_SCRIPT = (
    _ROOT / "scripts/vivado/export_logic_segment_timing.tcl"
)
def write_vivado_boundary_timing_query(
    ir_path: Path, identity_path: Path, output_path: Path,
) -> Dict[str, Any]:
    """Publish a source-owned native query generation and return its location."""
    from emuflow.physical.vivado.vivado_timing_query_native import write_vivado_boundary_query
    return write_vivado_boundary_query(ir_path, identity_path, output_path)


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
    from emuflow.ir.emuir_compact import is_compact_emuir
    from emuflow.ir.indexed_physical_ir import IndexedPhysicalIR

    from emuflow.ir.emuir_storage import is_emuir_sqlite

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
