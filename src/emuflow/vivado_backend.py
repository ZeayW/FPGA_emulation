"""Shared Vivado query, clock and scalar tool contracts."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .boundary_timing import BOUNDARY_IDENTITY_SCHEMA
from .boundary_identity_storage import read_boundary_identity_storage
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


def _vivado_mapped_name(value: str) -> str:
    """Return the exact NAME property Vivado exposes for a Yosys identifier."""
    return value.replace("\\", "\\\\")


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
    dut_frequency_mhz: float | None = None,
    dut_timing_io: Mapping[str, Any] | None = None,
) -> str:
    """Implement against explicit shared DUT targets, independently of frame length."""
    from .emuir_compact import is_compact_emuir
    from .indexed_physical_ir import IndexedPhysicalIR
    from .vivado_clocks import IMPLEMENTATION_CLOCK_POLICY, implementation_clocks

    from .emuir_storage import is_emuir_sqlite

    if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
        with IndexedPhysicalIR(ir_path) as ir:
            port_records = {item["id"]:item for item in ir.value["ports"]}
    else:
        port_records = {item["id"]:item for item in EmuIR.load(ir_path).value["ports"]}
    ports = set(port_records)
    lines = [f"# EmuFlow {IMPLEMENTATION_CLOCK_POLICY} physical implementation contract.",
             "# Virtual frame periods and settle windows are runtime analysis inputs,",
             "# not implementation clock periods or blanket timing exceptions."]
    clocks = implementation_clocks(runtime, ports, dut_frequency_mhz)
    for clock_name, port, period in clocks:
        lines.append(f"create_clock -name {clock_name} -period {period:.9f} [get_ports {{{port}}}]")
    if dut_timing_io is not None:
        from .benchmark import _validate_timing_io
        roots = runtime['timing_model']['dut_clock_ports']
        _validate_timing_io(dut_timing_io, list(roots))
        clock_names = {port:name for name,port,_period in clocks}
        lines.append('# Explicit original DUT setup I/O environment; delays remain in ns.')
        for direction in ('input','output'):
            for group in dut_timing_io[direction+'_groups']:
                for port in group['ports']:
                    item = port_records.get(port)
                    if item is None:
                        continue  # A partition contains only its original I/O subset.
                    if item['direction'] != direction:
                        raise ValidationError('Vivado DUT timing I/O direction differs: '+port)
                    if group['clock'] not in clock_names:
                        raise ValidationError('Vivado DUT timing I/O lacks its declared clock root: '+port)
                    select = port if item['width']==1 else port+'[*]'
                    lines.append(f"set_{direction}_delay -max {float(group['delay_ns']):.17g} "
                        f"-clock [get_clocks {{{clock_names[group['clock']]}}}] [get_ports {{{select}}}]")
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
