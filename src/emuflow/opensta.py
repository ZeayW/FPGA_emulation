"""OpenSTA-backed, partition-independent FPGA timing-path extraction."""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, Mapping, Optional, Sequence

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .sta import (
    import_sta_path_database_tsv,
    validate_sta_path_database,
    write_emuir_net_map,
)
from .verilog import mapped_verilog
from .vtr_architecture import (
    ARCHITECTURE_TIMING_DB_SCHEMA,
    validate_vtr_timing_db,
)


FPGA_TIMING_MODEL_SCHEMA = "emuflow.fpga-timing-model/v1"
FPGA_TIMING_MODEL_SCHEMA_V2 = "emuflow.fpga-timing-model/v2"
OPENSTA_PROVIDER = "opensta-fpga-path-database-v1"
OPENSTA_THROUGH_COVERAGE_SCHEMA = "emuflow.opensta-through-net-coverage/v1"


def _runtime_data_path(relative: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    candidates = (
        root / relative,
        root / "share" / "emuflow" / relative,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


DEFAULT_TIMING_MODEL = _runtime_data_path(
    Path("resources/timing/ultrascaleplus-softlogic-v1.json")
)
OPENSTA_EXPORT_SCRIPT = _runtime_data_path(
    Path("scripts/opensta/export_timing_path_database.tcl")
)


def _finite_nonnegative(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValidationError(f"{context}: expected a non-negative number")
    return float(value)


def load_timing_model(path: Path) -> Dict[str, Any]:
    value = read_json(path)
    if value.get("schema") not in {
        FPGA_TIMING_MODEL_SCHEMA,
        FPGA_TIMING_MODEL_SCHEMA_V2,
    }:
        raise ValidationError(
            "timing_model.schema: expected a supported FPGA timing model"
        )
    for key in ("name", "family"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValidationError(
                f"timing_model.{key}: expected a non-empty string"
            )
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValidationError("timing_model.source: expected an object")
    if source.get("qualification") not in {
        "analytical_uncharacterized",
        "academic_open_model",
        "calibrated",
        "characterized",
    }:
        raise ValidationError(
            "timing_model.source.qualification: unsupported value"
        )
    cells = value.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ValidationError("timing_model.cells: expected a non-empty object")
    for cell_name, raw_cell in sorted(cells.items()):
        context = f"timing_model.cells[{cell_name!r}]"
        if not isinstance(cell_name, str) or not cell_name:
            raise ValidationError("timing_model.cells: invalid cell name")
        if not isinstance(raw_cell, dict):
            raise ValidationError(f"{context}: expected an object")
        kind = raw_cell.get("kind")
        if kind == "combinational":
            inputs = raw_cell.get("inputs")
            outputs = raw_cell.get("outputs")
            if outputs is None and isinstance(raw_cell.get("output"), str):
                outputs = [raw_cell["output"]]
            if (
                not isinstance(inputs, list)
                or not inputs
                or not all(isinstance(pin, str) and pin for pin in inputs)
                or len(inputs) != len(set(inputs))
                or not isinstance(outputs, list)
                or not outputs
                or not all(isinstance(pin, str) and pin for pin in outputs)
                or len(outputs) != len(set(outputs))
                or set(outputs) & set(inputs)
            ):
                raise ValidationError(f"{context}: invalid pin definition")
            _finite_nonnegative(raw_cell.get("delay_ns"), f"{context}.delay_ns")
        elif kind == "rising_edge_ff":
            pins = [
                raw_cell.get("clock"),
                raw_cell.get("data"),
                raw_cell.get("output"),
            ]
            controls = raw_cell.get("controls")
            if (
                not all(isinstance(pin, str) and pin for pin in pins)
                or len(pins) != len(set(pins))
                or not isinstance(controls, list)
                or not all(
                    isinstance(pin, str) and pin for pin in controls
                )
                or len(controls) != len(set(controls))
                or set(pins) & set(controls)
            ):
                raise ValidationError(f"{context}: invalid pin definition")
            _finite_nonnegative(
                raw_cell.get("setup_ns"), f"{context}.setup_ns"
            )
            _finite_nonnegative(
                raw_cell.get("clock_to_q_ns"),
                f"{context}.clock_to_q_ns",
            )
        elif kind == "rising_edge_bank":
            clock = raw_cell.get("clock")
            inputs = raw_cell.get("inputs")
            outputs = raw_cell.get("outputs")
            if (
                not isinstance(clock, str)
                or not clock
                or not isinstance(inputs, list)
                or not inputs
                or not all(isinstance(pin, str) and pin for pin in inputs)
                or len(inputs) != len(set(inputs))
                or not isinstance(outputs, list)
                or not outputs
                or not all(isinstance(pin, str) and pin for pin in outputs)
                or len(outputs) != len(set(outputs))
                or clock in inputs
                or clock in outputs
                or set(inputs) & set(outputs)
            ):
                raise ValidationError(f"{context}: invalid pin definition")
            _finite_nonnegative(
                raw_cell.get("setup_ns"), f"{context}.setup_ns"
            )
            _finite_nonnegative(
                raw_cell.get("clock_to_q_ns"),
                f"{context}.clock_to_q_ns",
            )
        elif kind == "constant":
            outputs = raw_cell.get("outputs")
            if outputs is None and isinstance(raw_cell.get("output"), str):
                outputs = [raw_cell["output"]]
            if (
                not isinstance(outputs, list)
                or not outputs
                or not all(isinstance(pin, str) and pin for pin in outputs)
                or len(outputs) != len(set(outputs))
            ):
                raise ValidationError(f"{context}: invalid output pin")
        else:
            raise ValidationError(f"{context}.kind: unsupported value {kind!r}")
    return value


def _scalar_table(name: str, value: float, indent: str) -> list[str]:
    return [
        f"{indent}{name} (scalar) {{",
        f'{indent}  values ("{value:.12g}");',
        f"{indent}}}",
    ]


def render_opensta_liberty(model: Mapping[str, Any]) -> str:
    """Render the validated open timing model into deterministic Liberty."""
    name = str(model["name"]).replace("-", "_")
    lines = [
        f"library ({name}) {{",
        '  delay_model : "table_lookup";',
        '  time_unit : "1ns";',
        '  voltage_unit : "1V";',
        '  current_unit : "1mA";',
        '  leakage_power_unit : "1nW";',
        '  pulling_resistance_unit : "1kohm";',
        "  capacitive_load_unit (1,pF);",
        "  input_threshold_pct_rise : 50;",
        "  input_threshold_pct_fall : 50;",
        "  output_threshold_pct_rise : 50;",
        "  output_threshold_pct_fall : 50;",
        "  slew_lower_threshold_pct_rise : 20;",
        "  slew_lower_threshold_pct_fall : 20;",
        "  slew_upper_threshold_pct_rise : 80;",
        "  slew_upper_threshold_pct_fall : 80;",
        "",
    ]
    for cell_name, cell in sorted(model["cells"].items()):
        lines.extend([f"  cell ({cell_name}) {{", "    area : 1.0;"])
        kind = cell["kind"]
        if kind == "combinational":
            for pin in cell["inputs"]:
                lines.extend(
                    [
                        f"    pin ({pin}) {{",
                        "      direction : input;",
                        "      capacitance : 0.001;",
                        "    }",
                    ]
                )
            outputs = cell.get("outputs", [cell.get("output")])
            for output in outputs:
                lines.extend(
                    [
                        f"    pin ({output}) {{",
                        "      direction : output;",
                    ]
                )
                for pin in cell["inputs"]:
                    lines.extend(
                        [
                            "      timing () {",
                            f'        related_pin : "{pin}";',
                            "        timing_sense : non_unate;",
                            *_scalar_table(
                                "cell_rise",
                                float(cell["delay_ns"]),
                                "        ",
                            ),
                            *_scalar_table(
                                "cell_fall",
                                float(cell["delay_ns"]),
                                "        ",
                            ),
                            *_scalar_table(
                                "rise_transition", 0.01, "        "
                            ),
                            *_scalar_table(
                                "fall_transition", 0.01, "        "
                            ),
                            "      }",
                        ]
                    )
                lines.append("    }")
        elif kind == "rising_edge_ff":
            lines.extend(
                [
                    "    ff (IQ, IQN) {",
                    f'      clocked_on : "{cell["clock"]}";',
                    f'      next_state : "{cell["data"]}";',
                    "    }",
                    f"    pin ({cell['clock']}) {{",
                    "      direction : input;",
                    "      clock : true;",
                    "      capacitance : 0.001;",
                    "    }",
                    f"    pin ({cell['data']}) {{",
                    "      direction : input;",
                    "      capacitance : 0.001;",
                    "      timing () {",
                    f'        related_pin : "{cell["clock"]}";',
                    "        timing_type : setup_rising;",
                    *_scalar_table(
                        "rise_constraint",
                        float(cell["setup_ns"]),
                        "        ",
                    ),
                    *_scalar_table(
                        "fall_constraint",
                        float(cell["setup_ns"]),
                        "        ",
                    ),
                    "      }",
                    "    }",
                ]
            )
            for pin in cell["controls"]:
                lines.extend(
                    [
                        f"    pin ({pin}) {{",
                        "      direction : input;",
                        "      capacitance : 0.001;",
                        "    }",
                    ]
                )
            lines.extend(
                [
                    f"    pin ({cell['output']}) {{",
                    "      direction : output;",
                    '      function : "IQ";',
                    "      timing () {",
                    f'        related_pin : "{cell["clock"]}";',
                    "        timing_type : rising_edge;",
                    "        timing_sense : non_unate;",
                    *_scalar_table(
                        "cell_rise",
                        float(cell["clock_to_q_ns"]),
                        "        ",
                    ),
                    *_scalar_table(
                        "cell_fall",
                        float(cell["clock_to_q_ns"]),
                        "        ",
                    ),
                    *_scalar_table(
                        "rise_transition", 0.01, "        "
                    ),
                    *_scalar_table(
                        "fall_transition", 0.01, "        "
                    ),
                    "      }",
                    "    }",
                ]
            )
        elif kind == "rising_edge_bank":
            clock = cell["clock"]
            lines.extend(
                [
                    f"    pin ({clock}) {{",
                    "      direction : input;",
                    "      clock : true;",
                    "      capacitance : 0.001;",
                    "    }",
                ]
            )
            for pin in cell["inputs"]:
                lines.extend(
                    [
                        f"    pin ({pin}) {{",
                        "      direction : input;",
                        "      capacitance : 0.001;",
                        "      timing () {",
                        f'        related_pin : "{clock}";',
                        "        timing_type : setup_rising;",
                        *_scalar_table(
                            "rise_constraint",
                            float(cell["setup_ns"]),
                            "        ",
                        ),
                        *_scalar_table(
                            "fall_constraint",
                            float(cell["setup_ns"]),
                            "        ",
                        ),
                        "      }",
                        "    }",
                    ]
                )
            next_state = cell["inputs"][0]
            for index, pin in enumerate(cell["outputs"]):
                state = f"IQ{index}"
                state_n = f"IQN{index}"
                lines.extend(
                    [
                        f"    ff ({state}, {state_n}) {{",
                        f'      clocked_on : "{clock}";',
                        f'      next_state : "{next_state}";',
                        "    }",
                        f"    pin ({pin}) {{",
                        "      direction : output;",
                        f'      function : "{state}";',
                        "      timing () {",
                        f'        related_pin : "{clock}";',
                        "        timing_type : rising_edge;",
                        "        timing_sense : non_unate;",
                        *_scalar_table(
                            "cell_rise",
                            float(cell["clock_to_q_ns"]),
                            "        ",
                        ),
                        *_scalar_table(
                            "cell_fall",
                            float(cell["clock_to_q_ns"]),
                            "        ",
                        ),
                        *_scalar_table(
                            "rise_transition", 0.01, "        "
                        ),
                        *_scalar_table(
                            "fall_transition", 0.01, "        "
                        ),
                        "      }",
                        "    }",
                    ]
                )
        else:
            for output in cell.get("outputs", [cell.get("output")]):
                lines.extend(
                    [
                        f"    pin ({output}) {{",
                        "      direction : output;",
                        "    }",
                    ]
                )
        lines.extend(["  }", ""])
    lines.extend(["}", ""])
    return "\n".join(lines)


def _matrix_max_seconds(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    numbers = []
    for token in value.split():
        try:
            number = float(token)
        except ValueError as error:
            raise ValidationError(
                f"VTR timing matrix contains invalid number {token!r}"
            ) from error
        if not math.isfinite(number) or number < 0.0:
            raise ValidationError("VTR timing matrix contains invalid delay")
        numbers.append(number)
    return max(numbers, default=0.0)


def _arc_max_seconds(arc: Mapping[str, Any]) -> float:
    return max(
        float(arc.get("max_seconds", 0.0)),
        _matrix_max_seconds(arc.get("matrix", "")),
    )


def _instance_pin_sets(
    ir: EmuIR,
) -> Dict[str, Dict[str, set[tuple[str, int]]]]:
    result = {
        instance["id"]: {"inputs": set(), "outputs": set()}
        for instance in ir.value["instances"]
    }
    for net in ir.value["nets"]:
        for endpoint in net["drivers"]:
            if endpoint["instance"] is not None:
                result[endpoint["instance"]]["outputs"].add(
                    (endpoint["port"], endpoint["bit"])
                )
        for endpoint in net["sinks"]:
            if endpoint["instance"] is not None:
                result[endpoint["instance"]]["inputs"].add(
                    (endpoint["port"], endpoint["bit"])
                )
    for instance in ir.value["instances"]:
        for connection in instance.get("constant_connections", []):
            result[instance["id"]]["inputs"].add(
                (connection["port"], connection["bit"])
            )
    return result


def _scalar_pin_names(
    pins: Sequence[tuple[str, int]],
) -> list[str]:
    width_by_port: Dict[str, int] = {}
    for port, bit in pins:
        width_by_port[port] = max(width_by_port.get(port, 0), bit + 1)
    return [
        port if width_by_port[port] == 1 else f"{port}__{bit}"
        for port, bit in sorted(pins)
    ]


def _integer_parameter(
    instance: Mapping[str, Any],
    name: str,
    fallback: int,
) -> int:
    raw = instance.get("parameters", {}).get(name)
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw:
        try:
            if all(character in "01" for character in raw):
                return int(raw, 2)
            return int(raw)
        except ValueError:
            return fallback
    return fallback


def _vtr_timing_cell_type(
    instance: Mapping[str, Any],
    pins: Mapping[str, set[tuple[str, int]]],
) -> str:
    kind = instance["type"]
    all_pins = pins["inputs"] | pins["outputs"]

    def width(port: str) -> int:
        return max(
            (bit + 1 for candidate, bit in all_pins if candidate == port),
            default=0,
        )
    if kind == "$lut":
        lut_width = _integer_parameter(instance, "WIDTH", width("A"))
        return f"EMUFLOW_VTR_LUT{lut_width}"
    if kind.startswith("LUT") and kind[3:].isdigit():
        return f"EMUFLOW_VTR_LUT{int(kind[3:])}"
    if kind.startswith("$_DFF_"):
        return "EMUFLOW_VTR_DFF"
    if kind in {"FDCE", "FDPE", "FDRE", "FDSE"}:
        # Keep control-pin variants as distinct Liberty cells.  They share
        # the public VTR DFF setup/clock-to-Q characterization, but merging
        # them would create an inconsistent pin contract (CLR/PRE/R/S).
        return f"EMUFLOW_VTR_{kind}"
    if kind == "VTR_MULTIPLY":
        return (
            "EMUFLOW_VTR_MULTIPLY_"
            f"{_integer_parameter(instance, 'A_WIDTH', width('a'))}_"
            f"{_integer_parameter(instance, 'B_WIDTH', width('b'))}_"
            f"{_integer_parameter(instance, 'Y_WIDTH', width('out'))}"
        )
    if kind in {"VTR_SP_RAM", "VTR_DP_RAM"}:
        prefix = "SP" if kind == "VTR_SP_RAM" else "DP"
        return (
            f"EMUFLOW_VTR_{prefix}_RAM_"
            f"A{_integer_parameter(instance, 'ADDR_WIDTH', width('addr'))}_"
            f"D{_integer_parameter(instance, 'DATA_WIDTH', width('data'))}"
        )
    raise ValidationError(
        "VTR academic timing conversion does not cover mapped primitive "
        f"{kind!r}"
    )


def _vtr_sink_interconnect_delay_seconds(
    timing_db: Mapping[str, Any],
) -> float:
    """Derive a placement-independent sink delay from public VTR data.

    The estimate is one worst internal block arc plus the fastest ordinary
    routing switch and its segment RC. It is architecture-sourced but remains
    explicitly pre-placement; VPR later replaces it with routed timing.
    """
    block_delay = max(
        (_arc_max_seconds(arc)
         for arc in timing_db["block_interconnect_arcs"]),
        default=0.0,
    )
    switches = timing_db["routing"]["switches"]
    switch_by_name = {switch["name"]: switch for switch in switches}
    candidates = []
    for segment in timing_db["routing"]["segments"]:
        switch = switch_by_name.get(segment.get("mux"))
        if switch is None:
            continue
        segment_resistance = (
            float(segment["metal_resistance_ohm"]) * int(segment["length"])
        )
        segment_capacitance = (
            float(segment["metal_capacitance_f"]) * int(segment["length"])
        )
        switch_capacitance = float(switch["output_capacitance_f"])
        rc_delay = (
            float(switch["resistance_ohm"])
            * (segment_capacitance + switch_capacitance)
            + 0.5 * segment_resistance * segment_capacitance
        )
        candidates.append(float(switch["intrinsic_delay_seconds"]) + rc_delay)
    if not candidates:
        candidates = [
            float(switch["intrinsic_delay_seconds"]) for switch in switches
        ]
    return block_delay + min(candidates, default=0.0)


def build_vtr_opensta_timing_model(
    ir: EmuIR,
    timing_db_path: Path,
    output_path: Optional[Path] = None,
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """Translate an open VTR TimingDB to design-specialized OpenSTA cells."""
    timing_db = read_json(timing_db_path)
    if timing_db.get("schema") != ARCHITECTURE_TIMING_DB_SCHEMA:
        raise ValidationError("VTR OpenSTA conversion requires TimingDB v1")
    validate_vtr_timing_db(timing_db)

    primitive_by_scope = {
        primitive["path"]: primitive for primitive in timing_db["primitives"]
    }
    delays_by_cell: DefaultDict[str, list[float]] = defaultdict(list)
    setup_by_cell: DefaultDict[str, list[float]] = defaultdict(list)
    clock_to_q_by_cell: DefaultDict[str, list[float]] = defaultdict(list)
    model_by_cell: DefaultDict[str, set[str]] = defaultdict(set)
    for primitive in timing_db["primitives"]:
        model_by_cell[primitive["cell"]].add(primitive["model"])
    for arc in timing_db["primitive_arcs"]:
        primitive = primitive_by_scope.get(arc["scope"])
        if primitive is None:
            raise ValidationError(
                f"VTR timing arc has unknown primitive scope {arc['scope']!r}"
            )
        delay = _arc_max_seconds(arc)
        kind = arc["kind"]
        if kind in {"delay_constant", "delay_matrix"}:
            delays_by_cell[primitive["cell"]].append(delay)
        elif kind == "T_setup":
            setup_by_cell[primitive["cell"]].append(delay)
        elif kind == "T_clock_to_Q":
            clock_to_q_by_cell[primitive["cell"]].append(delay)

    interconnect_seconds = _vtr_sink_interconnect_delay_seconds(timing_db)
    pin_sets = _instance_pin_sets(ir)
    instance_cell_types = {
        instance["id"]: _vtr_timing_cell_type(
            instance, pin_sets[instance["id"]]
        )
        for instance in ir.value["instances"]
    }
    groups: DefaultDict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for instance in ir.value["instances"]:
        groups[instance_cell_types[instance["id"]]].append(instance)

    def maximum(values: Iterable[float], context: str) -> float:
        items = list(values)
        if not items:
            raise ValidationError(
                f"VTR TimingDB has no usable {context} timing arc"
            )
        return max(items)

    lut_cells = {
        int(cell[3:]): cell
        for cell in delays_by_cell
        if cell.startswith("LUT") and cell[3:].isdigit()
    }
    multiply_cells = {
        cell for cell, models in model_by_cell.items()
        if ".subckt multiply" in models
    }
    memory_cells = {
        cell for cell, models in model_by_cell.items()
        if models & {".subckt single_port_ram", ".subckt dual_port_ram"}
    }
    cells: Dict[str, Dict[str, Any]] = {}
    for timing_type, instances in sorted(groups.items()):
        representative = instances[0]
        representative_pins = pin_sets[representative["id"]]
        inputs = _scalar_pin_names(representative_pins["inputs"])
        outputs = _scalar_pin_names(representative_pins["outputs"])
        if any(
            _scalar_pin_names(pin_sets[instance["id"]]["inputs"]) != inputs
            or _scalar_pin_names(pin_sets[instance["id"]]["outputs"]) != outputs
            for instance in instances[1:]
        ):
            raise ValidationError(
                f"VTR timing cell {timing_type!r} has inconsistent pin widths"
            )
        kind = representative["type"]
        if kind == "$lut" or (
            kind.startswith("LUT") and kind[3:].isdigit()
        ):
            width = (
                int(kind[3:])
                if kind != "$lut"
                else _integer_parameter(
                    representative,
                    "WIDTH",
                    len([pin for pin in inputs if pin.startswith("A")]),
                )
            )
            compatible = [
                cell for candidate_width, cell in lut_cells.items()
                if candidate_width >= width
            ]
            if not compatible:
                compatible = list(lut_cells.values())
            cell_delay = maximum(
                (
                    delay
                    for cell in compatible
                    for delay in delays_by_cell[cell]
                ),
                f"LUT{width}",
            )
            cells[timing_type] = {
                "kind": "combinational",
                "inputs": inputs,
                "outputs": outputs,
                "delay_ns": (cell_delay + interconnect_seconds) * 1.0e9,
            }
        elif kind.startswith("$_DFF_") or kind in {
            "FDCE",
            "FDPE",
            "FDRE",
            "FDSE",
        }:
            setup = maximum(setup_by_cell["DFF"], "DFF setup")
            clock_to_q = maximum(
                clock_to_q_by_cell["DFF"], "DFF clock-to-Q"
            )
            clock = next(
                (pin for pin in inputs if pin in {"C", "CLK", "clk"}),
                None,
            )
            data = next((pin for pin in inputs if pin == "D"), None)
            if clock is None or data is None or len(outputs) != 1:
                raise ValidationError("VTR DFF mapped pin contract is invalid")
            cells[timing_type] = {
                "kind": "rising_edge_ff",
                "clock": clock,
                "data": data,
                "output": outputs[0],
                "controls": [
                    pin for pin in inputs if pin not in {clock, data}
                ],
                "setup_ns": (setup + interconnect_seconds) * 1.0e9,
                "clock_to_q_ns": clock_to_q * 1.0e9,
            }
        elif kind == "VTR_MULTIPLY":
            cell_delay = maximum(
                (
                    delay
                    for cell in multiply_cells
                    for delay in delays_by_cell[cell]
                ),
                "multiply",
            )
            cells[timing_type] = {
                "kind": "combinational",
                "inputs": inputs,
                "outputs": outputs,
                "delay_ns": (cell_delay + interconnect_seconds) * 1.0e9,
            }
        else:
            setup = maximum(
                (
                    delay
                    for cell in memory_cells
                    for delay in setup_by_cell[cell]
                ),
                "memory setup",
            )
            clock_to_q = maximum(
                (
                    delay
                    for cell in memory_cells
                    for delay in clock_to_q_by_cell[cell]
                ),
                "memory clock-to-Q",
            )
            clock = next(
                (pin for pin in inputs if pin in {"C", "CLK", "clk"}),
                None,
            )
            if clock is None:
                raise ValidationError("VTR RAM mapped clock pin is absent")
            cells[timing_type] = {
                "kind": "rising_edge_bank",
                "clock": clock,
                "inputs": [pin for pin in inputs if pin != clock],
                "outputs": outputs,
                "setup_ns": (setup + interconnect_seconds) * 1.0e9,
                "clock_to_q_ns": clock_to_q * 1.0e9,
            }

    model = {
        "schema": FPGA_TIMING_MODEL_SCHEMA_V2,
        "name": f"{timing_db['name']}-opensta-preplacement",
        "family": f"vtr:{timing_db['name']}",
        "source": {
            "provider": "vtr-timingdb-opensta-translator-v1",
            "qualification": "academic_open_model",
            "architecture": timing_db["architecture"],
            "architecture_timing_db": str(timing_db_path),
            "architecture_source_sha256": timing_db["source"]["sha256"],
            "interconnect_model": "vtr-preplacement-sink-delay-v1",
            "sink_interconnect_delay_ns": interconnect_seconds * 1.0e9,
        },
        "cells": cells,
    }
    if output_path is not None:
        write_json(output_path, model)
    load_timing_model(output_path) if output_path is not None else None
    return model, instance_cell_types


def validate_timing_model_coverage(
    ir: EmuIR,
    model: Mapping[str, Any],
    instance_cell_types: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    used = sorted({
        (
            instance_cell_types.get(instance["id"], instance["type"])
            if instance_cell_types is not None
            else instance["type"]
        )
        for instance in ir.value["instances"]
    })
    supported = set(model["cells"])
    unsupported = sorted(set(used) - supported)
    if unsupported:
        raise ValidationError(
            "OpenSTA timing model does not cover mapped primitives: "
            f"{unsupported}"
        )
    return {
        "status": "pass",
        "used_cell_types": used,
        "model_cell_types": sorted(supported),
    }


def _scalar_endpoint_pin(
    endpoint: Mapping[str, Any],
    pin_sets: Mapping[str, Mapping[str, set[tuple[str, int]]]],
) -> str:
    instance = endpoint["instance"]
    if instance is None:
        return endpoint["port"]
    pins = pin_sets[instance]["inputs"] | pin_sets[instance]["outputs"]
    width = max(
        (
            bit + 1
            for port, bit in pins
            if port == endpoint["port"]
        ),
        default=0,
    )
    if width <= endpoint["bit"]:
        raise ValidationError(
            "OpenSTA endpoint pin is absent from the mapped pin contract"
        )
    return (
        endpoint["port"]
        if width == 1
        else f"{endpoint['port']}__{endpoint['bit']}"
    )


def classify_through_net_timing_endpoints(
    ir: EmuIR,
    model: Mapping[str, Any],
    through_nets: Sequence[str],
    instance_cell_types: Optional[Mapping[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Independently classify whether each net lies on a complete timed path.

    This graph walk uses only EmuIR connectivity and the validated timing-cell
    contract.  It intentionally does not consume OpenSTA's query results, so a
    zero-path result is accepted only when a second implementation proves that
    no sequential startpoint-to-data/setup path crosses the requested net.
    """
    net_by_id = {net["id"]: net for net in ir.value["nets"]}
    if len(net_by_id) != len(ir.value["nets"]):
        raise ValidationError("OpenSTA structural timing net IDs are not unique")
    unknown = sorted(set(through_nets) - set(net_by_id))
    if unknown:
        raise ValidationError(
            f"OpenSTA structural timing nets are absent from EmuIR: {unknown}"
        )
    instance_by_id = {
        instance["id"]: instance for instance in ir.value["instances"]
    }
    pin_sets = _instance_pin_sets(ir)
    output_nets: DefaultDict[tuple[str, str], list[str]] = defaultdict(list)
    for net in ir.value["nets"]:
        for driver in net["drivers"]:
            if driver["instance"] is not None:
                output_nets[
                    (
                        driver["instance"],
                        _scalar_endpoint_pin(driver, pin_sets),
                    )
                ].append(net["id"])

    forward_edges: DefaultDict[str, set[str]] = defaultdict(set)
    reverse_edges: DefaultDict[str, set[str]] = defaultdict(set)
    timing_startpoints: set[str] = set()
    direct_timed: set[str] = set()
    direct_timed_counts: DefaultDict[str, int] = defaultdict(int)
    direct_timed_pins: DefaultDict[str, set[str]] = defaultdict(set)
    top_level_sink_counts: DefaultDict[str, int] = defaultdict(int)
    for net_id, net in net_by_id.items():
        for driver in net["drivers"]:
            if driver["instance"] is None:
                continue
            instance_id = driver["instance"]
            instance = instance_by_id[instance_id]
            cell_type = (
                instance_cell_types.get(instance_id, instance["type"])
                if instance_cell_types is not None
                else instance["type"]
            )
            cell = model["cells"].get(cell_type)
            if cell is None:
                raise ValidationError(
                    f"OpenSTA structural timing model lacks {cell_type!r}"
                )
            pin = _scalar_endpoint_pin(driver, pin_sets)
            kind = cell["kind"]
            if (
                (kind == "rising_edge_ff" and pin == cell["output"])
                or (kind == "rising_edge_bank" and pin in cell["outputs"])
            ):
                timing_startpoints.add(net_id)

        for sink in net["sinks"]:
            if sink["instance"] is None:
                top_level_sink_counts[net_id] += 1
                continue
            instance_id = sink["instance"]
            instance = instance_by_id[instance_id]
            cell_type = (
                instance_cell_types.get(instance_id, instance["type"])
                if instance_cell_types is not None
                else instance["type"]
            )
            cell = model["cells"].get(cell_type)
            if cell is None:
                raise ValidationError(
                    f"OpenSTA structural timing model lacks {cell_type!r}"
                )
            pin = _scalar_endpoint_pin(sink, pin_sets)
            kind = cell["kind"]
            if kind == "combinational":
                if pin not in cell["inputs"]:
                    raise ValidationError(
                        "OpenSTA combinational sink pin is absent from the "
                        f"timing model: {cell_type}.{pin}"
                    )
                for output in cell.get("outputs", [cell.get("output")]):
                    for successor in output_nets.get((instance_id, output), ()):
                        forward_edges[net_id].add(successor)
                        reverse_edges[successor].add(net_id)
            elif kind == "rising_edge_ff":
                if pin == cell["data"]:
                    direct_timed.add(net_id)
                    direct_timed_counts[net_id] += 1
                    direct_timed_pins[net_id].add(f"{instance_id}/{pin}")
                elif pin != cell["clock"] and pin not in cell["controls"]:
                    raise ValidationError(
                        f"OpenSTA FF sink pin is unmodelled: {cell_type}.{pin}"
                    )
            elif kind == "rising_edge_bank":
                if pin in cell["inputs"]:
                    direct_timed.add(net_id)
                    direct_timed_counts[net_id] += 1
                    direct_timed_pins[net_id].add(f"{instance_id}/{pin}")
                elif pin != cell["clock"]:
                    raise ValidationError(
                        "OpenSTA sequential-bank sink pin is unmodelled: "
                        f"{cell_type}.{pin}"
                    )
            elif kind != "constant":
                raise ValidationError(
                    f"OpenSTA structural timing kind is unsupported: {kind!r}"
                )

    reachable_from_startpoint = set(timing_startpoints)
    pending = list(timing_startpoints)
    while pending:
        predecessor = pending.pop()
        for successor in forward_edges.get(predecessor, ()):
            if successor not in reachable_from_startpoint:
                reachable_from_startpoint.add(successor)
                pending.append(successor)

    reaches_timed = set(direct_timed)
    pending = list(direct_timed)
    while pending:
        successor = pending.pop()
        for predecessor in reverse_edges.get(successor, ()):
            if predecessor not in reaches_timed:
                reaches_timed.add(predecessor)
                pending.append(predecessor)
    return {
        net: {
            "status": (
                "timed"
                if net in reachable_from_startpoint and net in reaches_timed
                else "no_timed_endpoint"
            ),
            "direct_timed_endpoints": direct_timed_counts[net],
            "direct_timed_endpoint_pins": sorted(direct_timed_pins[net]),
            "direct_top_level_sinks": top_level_sink_counts[net],
        }
        for net in through_nets
    }


def _read_through_coverage_tsv(
    path: Path, expected_nets: Sequence[str]
) -> Dict[str, Dict[str, int]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    expected_header = (
        "emuir_net_hex\tdriver_count\tqueried_paths\temitted_paths"
    )
    if not lines or lines[0] != expected_header:
        raise EmuFlowError("OpenSTA through-net coverage TSV header is invalid")
    records: Dict[str, Dict[str, int]] = {}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 4:
            raise EmuFlowError("OpenSTA through-net coverage TSV row is invalid")
        try:
            net = bytes.fromhex(fields[0]).decode("utf-8")
            counts = [int(value) for value in fields[1:]]
        except (UnicodeDecodeError, ValueError) as error:
            raise EmuFlowError(
                "OpenSTA through-net coverage TSV value is invalid"
            ) from error
        if net in records or any(value < 0 for value in counts):
            raise EmuFlowError(
                "OpenSTA through-net coverage TSV has duplicate or negative data"
            )
        records[net] = dict(
            zip(("driver_count", "queried_paths", "emitted_paths"), counts)
        )
    if set(records) != set(expected_nets):
        raise EmuFlowError(
            "OpenSTA through-net coverage TSV does not exactly cover requests"
        )
    return records


def _clock_map(
    ir: EmuIR,
    clocks: Optional[Mapping[str, float]],
) -> Dict[str, float]:
    available = {clock["id"]: clock for clock in ir.value["clocks"]}
    if clocks is None:
        result = {
            clock_id: float(clock["period_ns"])
            for clock_id, clock in available.items()
            if isinstance(clock.get("period_ns"), (int, float))
            and not isinstance(clock.get("period_ns"), bool)
        }
    else:
        result = dict(clocks)
    if not result:
        raise ValidationError(
            "OpenSTA requires at least one CLOCK=PERIOD_NS definition"
        )
    unknown = sorted(set(result) - set(available))
    if unknown:
        raise ValidationError(f"OpenSTA clocks are absent from EmuIR: {unknown}")
    for name, period in result.items():
        if (
            isinstance(period, bool)
            or not isinstance(period, (int, float))
            or not math.isfinite(float(period))
            or float(period) <= 0.0
        ):
            raise ValidationError(
                f"OpenSTA clock {name!r} period must be positive"
            )
    return {name: float(result[name]) for name in sorted(result)}


def parse_clock_definitions(values: Iterable[str]) -> Dict[str, float]:
    clocks: Dict[str, float] = {}
    for value in values:
        name, separator, raw_period = value.partition("=")
        if not separator or not name or not raw_period:
            raise ValidationError(
                f"--clock-period: expected CLOCK=PERIOD_NS, got {value!r}"
            )
        if name in clocks:
            raise ValidationError(
                f"--clock-period: duplicate clock {name!r}"
            )
        try:
            clocks[name] = float(raw_period)
        except ValueError as error:
            raise ValidationError(
                f"--clock-period: invalid period in {value!r}"
            ) from error
    return clocks


def run_opensta_path_database(
    ir_path: Path,
    output_path: Path,
    clocks: Optional[Mapping[str, float]] = None,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
    executable: Optional[str] = None,
    max_paths: int = 200000,
    log_path: Optional[Path] = None,
    through_nets: Optional[Sequence[str]] = None,
    through_coverage_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if max_paths <= 0:
        raise ValidationError("OpenSTA max_paths must be positive")
    ir = EmuIR.load(ir_path)
    through_net_ids = list(through_nets or [])
    if (
        any(not isinstance(net, str) or not net for net in through_net_ids)
        or len(through_net_ids) != len(set(through_net_ids))
    ):
        raise ValidationError("OpenSTA through_nets must be unique net IDs")
    net_index = {
        net["id"]: index for index, net in enumerate(ir.value["nets"])
    }
    unknown_through_nets = sorted(set(through_net_ids) - set(net_index))
    if unknown_through_nets:
        raise ValidationError(
            f"OpenSTA through_nets are absent from EmuIR: {unknown_through_nets}"
        )
    instance_cell_types: Optional[Dict[str, str]] = None
    if architecture_timing_db_path is not None:
        model, instance_cell_types = build_vtr_opensta_timing_model(
            ir, architecture_timing_db_path
        )
    else:
        model = load_timing_model(timing_model_path)
    coverage = validate_timing_model_coverage(
        ir, model, instance_cell_types
    )
    clock_map = _clock_map(ir, clocks)
    opensta = resolve_native_executable("sta", executable)
    structural = (
        classify_through_net_timing_endpoints(
            ir, model, through_net_ids, instance_cell_types
        )
        if through_net_ids
        else {}
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="emuflow-opensta-") as temporary:
        root = Path(temporary)
        verilog_path = root / "mapped.v"
        liberty_path = root / "timing.lib"
        net_map_path = root / "net-map.tsv"
        clock_path = root / "clocks.tsv"
        raw_path = root / "paths.tsv"
        through_path = root / "through-nets.tsv"
        through_endpoint_path = root / "through-endpoints.tsv"
        raw_through_coverage_path = root / "through-net-coverage.tsv"
        # OpenSTA's intentionally small Verilog reader does not accept net
        # declaration keywords on ports, attributes, or instance parameters.
        # Those constructs do not affect static timing connectivity.
        verilog_path.write_text(
            mapped_verilog(
                ir,
                timing_only=True,
                timing_cell_types=instance_cell_types,
            ),
            encoding="utf-8",
        )
        liberty_path.write_text(
            render_opensta_liberty(model), encoding="utf-8"
        )
        write_emuir_net_map(ir_path, net_map_path)
        if through_net_ids:
            with through_path.open("w", encoding="utf-8") as stream:
                stream.write("mapped_net_hex\temuir_net_hex\n")
                for net in through_net_ids:
                    mapped = f"__emuflow_net_{net_index[net]}"
                    stream.write(
                        f"{mapped.encode().hex()}\t{net.encode().hex()}\n"
                    )
            with through_endpoint_path.open("w", encoding="utf-8") as stream:
                stream.write("emuir_net_hex\tendpoint_pin_hex\n")
                for net in through_net_ids:
                    for pin in structural[net]["direct_timed_endpoint_pins"]:
                        stream.write(
                            f"{net.encode().hex()}\t{pin.encode().hex()}\n"
                        )
        with clock_path.open("w", encoding="utf-8") as stream:
            stream.write("clock_hex\tperiod_ns\n")
            for name, period in clock_map.items():
                stream.write(f"{name.encode().hex()}\t{period:.12g}\n")

        environment = os.environ.copy()
        environment.update(
            {
                "EMUFLOW_STA_LIBERTY": str(liberty_path),
                "EMUFLOW_STA_VERILOG": str(verilog_path),
                "EMUFLOW_STA_TOP": ir.value["design"]["top"],
                "EMUFLOW_STA_NET_MAP": str(net_map_path),
                "EMUFLOW_STA_CLOCKS": str(clock_path),
                "EMUFLOW_STA_OUTPUT": str(raw_path),
                "EMUFLOW_STA_MAX_PATHS": str(max_paths),
                "EMUFLOW_STA_THROUGH_NETS": (
                    str(through_path) if through_net_ids else ""
                ),
                "EMUFLOW_STA_THROUGH_COVERAGE": (
                    str(raw_through_coverage_path) if through_net_ids else ""
                ),
                "EMUFLOW_STA_THROUGH_ENDPOINTS": (
                    str(through_endpoint_path) if through_net_ids else ""
                ),
            }
        )
        completed = subprocess.run(
            [opensta, "-exit", str(OPENSTA_EXPORT_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=environment,
        )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(completed.stdout, encoding="utf-8")
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-30:])
            raise EmuFlowError(
                "OpenSTA path extraction failed with exit code "
                f"{completed.returncode}\n{tail}"
            )
        if not raw_path.is_file():
            raise EmuFlowError(
                "OpenSTA reported success but did not create its path TSV"
            )
        imported = import_sta_path_database_tsv(
            raw_path,
            ir_path,
            output_path,
            provider=OPENSTA_PROVIDER,
            source={
                "timing_model": model["name"],
                "timing_model_qualification": model["source"][
                    "qualification"
                ],
                "architecture_timing_db": (
                    str(architecture_timing_db_path)
                    if architecture_timing_db_path is not None
                    else None
                ),
            },
        )

        through_query_records = (
            _read_through_coverage_tsv(
                raw_through_coverage_path, through_net_ids
            )
            if through_net_ids
            else {}
        )

    checked = validate_sta_path_database(output_path, ir_path)
    covered_through_nets = []
    through_coverage: Dict[str, Any] | None = None
    if through_net_ids:
        database = read_json(output_path)
        path_nets = {
            net
            for path in database["paths"]
            for net in path["path_nets"]
        }
        coverage_records = []
        for net in through_net_ids:
            query = through_query_records[net]
            if query["driver_count"] <= 0:
                raise EmuFlowError(
                    f"OpenSTA through net {net!r} has no timing driver"
                )
            emitted = query["emitted_paths"]
            queried = query["queried_paths"]
            in_database = net in path_nets
            if emitted > queried:
                raise EmuFlowError(
                    f"OpenSTA through net {net!r} emitted more paths than queried"
                )
            if emitted > 0 and not in_database:
                raise EmuFlowError(
                    f"OpenSTA through net {net!r} was emitted without database coverage"
                )
            if emitted == 0 and queried > 0:
                raise EmuFlowError(
                    f"OpenSTA through net {net!r} had timing paths that were not serialized"
                )
            if emitted == 0 and structural[net]["status"] != "no_timed_endpoint":
                raise EmuFlowError(
                    f"OpenSTA through net {net!r} lies on a structurally "
                    "complete timed path but has no exported path"
                )
            if emitted > 0 and structural[net]["status"] != "timed":
                raise EmuFlowError(
                    f"OpenSTA through net {net!r} exported a path without a "
                    "structurally complete timed path"
                )
            coverage_records.append(
                {
                    "net": net,
                    **query,
                    "classification": (
                        "timed" if emitted > 0 else "no_timing_path"
                    ),
                    "structural": structural[net],
                }
            )
        covered_through_nets = sorted(set(through_net_ids) & path_nets)
        through_coverage = {
            "schema": OPENSTA_THROUGH_COVERAGE_SCHEMA,
            "status": "pass",
            "provider": OPENSTA_PROVIDER,
            "requested_nets": len(through_net_ids),
            "timed_nets": sum(
                record["classification"] == "timed"
                for record in coverage_records
            ),
            "untimed_nets": sum(
                record["classification"] == "no_timing_path"
                for record in coverage_records
            ),
            "records": coverage_records,
        }
        if through_coverage_path is not None:
            write_json(through_coverage_path, through_coverage)
    return {
        "status": "pass",
        "design": ir.value["design"]["name"],
        "provider": OPENSTA_PROVIDER,
        "timing_model": model["name"],
        "timing_model_qualification": model["source"]["qualification"],
        "clocks": clock_map,
        "paths": imported["paths"],
        "max_paths": max_paths,
        "through_nets": through_net_ids,
        "covered_through_nets": covered_through_nets,
        "through_net_coverage": through_coverage,
        "path_limit_reached": imported["paths"] >= max_paths,
        "unique_path_nets": imported["unique_path_nets"],
        "used_cell_types": coverage["used_cell_types"],
        "checker": checked,
        "output": str(output_path),
        "log": str(log_path) if log_path is not None else None,
    }
