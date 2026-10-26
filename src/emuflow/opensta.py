"""OpenSTA-backed, partition-independent FPGA timing-path extraction."""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, Mapping, Optional, Sequence

from .benchmark import timing_io_sha256
from .errors import EmuFlowError, ValidationError
from .io import file_sha256, read_json, write_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .process_output import run_with_bounded_output
from .sta import (
    import_sta_path_database_tsv,
    import_sta_path_database_tsv_streaming,
    validate_sta_path_database_path_value,
    write_emuir_net_map,
)
from .verilog import write_mapped_verilog
from .xilinx_preplacement_timing import (
    XILINX_PREPLACEMENT_TIMING_SCHEMA,
    validate_xilinx_preplacement_timing_db,
)


FPGA_TIMING_MODEL_SCHEMA = "emuflow.fpga-timing-model/v1"
FPGA_TIMING_MODEL_SCHEMA_V2 = "emuflow.fpga-timing-model/v2"
OPENSTA_PROVIDER = "opensta-fpga-path-database-v1"
OPENSTA_THROUGH_COVERAGE_SCHEMA = "emuflow.opensta-through-net-coverage/v1"
MINIMUM_OPENSTA_VERSION = (3, 1, 0)


def _write_timing_io_map(
    ir: EmuIR,
    clocks: Mapping[str, float],
    timing_io: Optional[Mapping[str, Any]],
    output_path: Path,
) -> Dict[str, Any]:
    """Validate and serialize the exact benchmark top-level timing context."""

    rows: list[tuple[str, str, float, str]] = []
    ports = {item["id"]: item for item in ir.value["ports"]}
    if timing_io is not None:
        if not isinstance(timing_io, Mapping) or set(timing_io) != {
            "input_groups",
            "output_groups",
        }:
            raise ValidationError("OpenSTA timing_io contract is invalid")
        seen: set[tuple[str, str]] = set()
        for direction in ("input", "output"):
            groups = timing_io[f"{direction}_groups"]
            if not isinstance(groups, list):
                raise ValidationError("OpenSTA timing_io groups are invalid")
            for group in groups:
                if not isinstance(group, Mapping) or set(group) != {
                    "clock",
                    "delay_ns",
                    "ports",
                }:
                    raise ValidationError("OpenSTA timing_io group is invalid")
                clock = group["clock"]
                delay = group["delay_ns"]
                declared_ports = group["ports"]
                if clock not in clocks:
                    raise ValidationError("OpenSTA timing_io clock is undeclared")
                if (
                    isinstance(delay, bool)
                    or not isinstance(delay, (int, float))
                    or not math.isfinite(float(delay))
                    or float(delay) < 0.0
                    or not isinstance(declared_ports, list)
                    or not declared_ports
                ):
                    raise ValidationError("OpenSTA timing_io group is invalid")
                for port in declared_ports:
                    item = ports.get(port) if isinstance(port, str) else None
                    key = (direction, port)
                    if (
                        item is None
                        or item.get("direction") != direction
                        or key in seen
                        or port in clocks
                    ):
                        raise ValidationError(
                            f"OpenSTA timing_io {direction} port {port!r} is invalid"
                        )
                    seen.add(key)
                    rows.append((direction, str(clock), float(delay), port))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as stream:
        stream.write("direction\tclock_hex\tdelay_ns\tport_hex\n")
        for direction, clock, delay, port in rows:
            stream.write(
                f"{direction}\t{clock.encode().hex()}\t{delay:.17g}\t"
                f"{port.encode().hex()}\n"
            )
    return {
        "sha256": timing_io_sha256(timing_io),
        "input_ports": sum(direction == "input" for direction, *_ in rows),
        "output_ports": sum(direction == "output" for direction, *_ in rows),
    }


def check_opensta_engine_version(executable: str) -> str:
    """Fail closed on a legacy engine without hashing the executable.

    One-shot flows use this lightweight probe before synthesis so an invalid
    runtime cannot waste the expensive frontend.  The authoritative provider
    invocation still calls :func:`require_opensta_engine` once, where the
    executable is content-sealed for retained evidence.
    """
    version_result = run_with_bounded_output([executable, "-version"])
    version_match = re.search(
        r"(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:\s|$)",
        version_result.stdout,
    )
    if version_result.returncode != 0 or version_match is None:
        raise ValidationError(
            "OpenSTA executable does not report a semantic version"
        )
    version_tuple = tuple(int(field) for field in version_match.groups())
    if version_tuple < MINIMUM_OPENSTA_VERSION:
        minimum = ".".join(str(field) for field in MINIMUM_OPENSTA_VERSION)
        actual = ".".join(str(field) for field in version_tuple)
        raise ValidationError(
            f"OpenSTA {actual} is unsupported; EmuFlow requires {minimum} or newer"
        )
    runtime_result = run_with_bounded_output(
        [executable, "-no_init", "-exit", str(OPENSTA_RUNTIME_CHECK_SCRIPT)]
    )
    if (runtime_result.returncode != 0 or
            "EMUFLOW_OPENSTA_RUNTIME status=pass" not in runtime_result.stdout):
        raise ValidationError(
            "OpenSTA runtime qualification failed; Tcl 8.6+ with try/finally "
            "is required: " + runtime_result.stdout[-1000:]
        )
    return ".".join(str(field) for field in version_tuple)


def require_opensta_engine(executable: str) -> Dict[str, str]:
    """Validate and content-seal the OpenSTA engine used for evidence."""
    version = check_opensta_engine_version(executable)
    selected = Path(executable).resolve()
    selected_digest = file_sha256(selected)
    identity = {
        "name": "OpenSTA",
        "version": version,
        "executable_sha256": selected_digest,
    }
    launcher = _runtime_data_path(Path("scripts/opensta/container_runtime.sh")).resolve()
    # CMake and wheel installs can expose identical repository launchers at
    # different paths. Recognize their exact bytes, never an ambient variable
    # alone or a merely similar shell-script filename.
    launcher_digest = selected_digest if selected == launcher else (
        file_sha256(launcher) if launcher.is_file() else None
    )
    if selected == launcher or selected_digest == launcher_digest:
        binding = os.environ.get("EMUFLOW_OPENSTA_CONTAINER_BINARY")
        if not binding:
            raise ValidationError("OpenSTA container engine identity lacks an explicit binary")
        binary = Path(binding).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValidationError("OpenSTA container engine identity is not executable")
        identity["executable_sha256"] = file_sha256(binary)
        identity["launcher_sha256"] = selected_digest
    return identity


def _runtime_data_path(relative: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    candidates = (
        root / relative,
        root / "share" / "emuflow" / relative,
        Path(sys.prefix) / "share" / "emuflow" / relative,
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
OPENSTA_RUNTIME_CHECK_SCRIPT = _runtime_data_path(
    Path("scripts/opensta/validate_runtime.tcl")
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
            controls = raw_cell.get("controls", [])
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
                or not isinstance(controls, list)
                or not all(isinstance(pin, str) and pin for pin in controls)
                or len(controls) != len(set(controls))
                or set(outputs) & (set(inputs) | set(controls))
                or set(inputs) & set(controls)
            ):
                raise ValidationError(f"{context}: invalid pin definition")
            _finite_nonnegative(raw_cell.get("delay_ns"), f"{context}.delay_ns")
            senses = raw_cell.get('timing_sense')
            if senses is not None and (not isinstance(senses, dict) or set(senses) != set(outputs) or any(
                not isinstance(arcs, dict) or not set(arcs) <= set(inputs) or any(
                    value not in {'positive_unate', 'negative_unate', 'non_unate'} for value in arcs.values())
                for arcs in senses.values())):
                raise ValidationError(f'{context}: invalid per-output arc polarity')
        elif kind in {"rising_edge_ff", "falling_edge_ff"}:
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
            controls = raw_cell.get("controls", [])
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
                or not isinstance(controls, list)
                or not all(isinstance(pin, str) and pin for pin in controls)
                or len(controls) != len(set(controls))
                or clock in inputs
                or clock in outputs
                or clock in controls
                or set(inputs) & set(outputs)
                or set(controls) & (set(inputs) | set(outputs))
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
            for pin in cell.get("controls", []):
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
                    senses = cell.get('timing_sense')
                    if senses is not None and pin not in senses[output]:
                        continue
                    sense = senses[output][pin] if senses is not None else 'non_unate'
                    lines.extend(
                        [
                            "      timing () {",
                            f'        related_pin : "{pin}";',
                            f"        timing_sense : {sense};",
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
        elif kind in {"rising_edge_ff", "falling_edge_ff"}:
            falling = kind == "falling_edge_ff"
            clocked_on = (
                f"!{cell['clock']}" if falling else cell["clock"]
            )
            setup_type = "setup_falling" if falling else "setup_rising"
            edge_type = "falling_edge" if falling else "rising_edge"
            lines.extend(
                [
                    "    ff (IQ, IQN) {",
                    f'      clocked_on : "{clocked_on}";',
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
                    f"        timing_type : {setup_type};",
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
                    f"        timing_type : {edge_type};",
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
            for pin in cell.get("controls", []):
                lines.extend(
                    [
                        f"    pin ({pin}) {{",
                        "      direction : input;",
                        "      capacitance : 0.001;",
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


def _instance_pin_sets(
    ir: EmuIR,
    *,
    instance_ids: Optional[set[str]] = None,
) -> Dict[str, Dict[str, set[tuple[str, int]]]]:
    if instance_ids is not None and not instance_ids:
        return {}
    result = {
        instance["id"]: {"inputs": set(), "outputs": set()}
        for instance in ir.value["instances"]
        if instance_ids is None or instance["id"] in instance_ids
    }
    for net in ir.value["nets"]:
        for endpoint in net["drivers"]:
            if endpoint["instance"] is not None and (
                instance_ids is None or endpoint["instance"] in result
            ):
                result[endpoint["instance"]]["outputs"].add(
                    (endpoint["port"], endpoint["bit"])
                )
        for endpoint in net["sinks"]:
            if endpoint["instance"] is not None and (
                instance_ids is None or endpoint["instance"] in result
            ):
                result[endpoint["instance"]]["inputs"].add(
                    (endpoint["port"], endpoint["bit"])
                )
    for instance in ir.value["instances"]:
        if instance["id"] not in result:
            continue
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


def build_xilinx_preplacement_opensta_timing_model(
    ir: EmuIR,
    timing_db_path: Path,
    output_path: Optional[Path] = None,
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """Build design-specialized cells from pinned RapidWright delay bounds."""

    if hasattr(ir, "representative_graph"):
        model, _ = build_xilinx_preplacement_opensta_timing_model(
            ir.representative_graph(), timing_db_path, output_path
        )
        return model, ir.timing_types()

    timing_db = read_json(timing_db_path)
    validate_xilinx_preplacement_timing_db(timing_db)
    delays = {
        name: float(value) / 1000.0
        for name, value in timing_db["delays_ps"].items()
    }
    model = deepcopy(load_timing_model(DEFAULT_TIMING_MODEL))
    for width in range(1, 7):
        model["cells"][f"LUT{width}"]["delay_ns"] = (
            delays[f"lut{width}"] + delays["sink_interconnect"]
        )
    for name in ("MUXF7", "MUXF8"):
        model["cells"][name]["delay_ns"] = (
            delays[name.lower()] + delays["sink_interconnect"]
        )
    for name in ("FDCE", "FDPE", "FDRE", "FDSE"):
        model["cells"][name]["setup_ns"] = (
            delays["ff_setup"] + delays["sink_interconnect"]
        )
        model["cells"][name]["clock_to_q_ns"] = delays["ff_clock_to_q"]

    # Cells already present in the library need no design-derived pin sets.
    # The producer below has always specialized each remaining type from its
    # first occurrence; collect only those representatives, not every cell.
    representatives: Dict[str, str] = {}
    for instance in ir.value["instances"]:
        if instance["type"] not in model["cells"]:
            representatives.setdefault(instance["type"], instance["id"])
    pin_sets = _instance_pin_sets(
        ir, instance_ids=set(representatives.values())
    )
    instance_cell_types = {
        instance["id"]: instance["type"]
        for instance in ir.value["instances"]
    }
    for instance in ir.value["instances"]:
        cell_type = instance["type"]
        if (
            cell_type in {"FDCE", "FDPE", "FDRE", "FDSE"}
            and _integer_parameter(instance, "IS_C_INVERTED", 0) == 1
        ):
            timing_type = f"{cell_type}__NEG"
            if timing_type not in model["cells"]:
                model["cells"][timing_type] = deepcopy(
                    model["cells"][cell_type]
                )
                model["cells"][timing_type]["kind"] = "falling_edge_ff"
            instance_cell_types[instance["id"]] = timing_type
    for instance in ir.value["instances"]:
        cell_type = instance["type"]
        if cell_type in model["cells"]:
            continue
        pins = pin_sets[instance["id"]]
        inputs = _scalar_pin_names(pins["inputs"])
        outputs = _scalar_pin_names(pins["outputs"])
        if cell_type == "LUT6_2":
            model["cells"][cell_type] = {
                "kind": "combinational",
                "inputs": inputs,
                "outputs": outputs,
                "delay_ns": delays["lut6_2"] + delays["sink_interconnect"],
            }
        elif cell_type in {"MUXF9", "CARRY8", "DSP48E2"}:
            if not inputs or not outputs:
                raise ValidationError(
                    f"Xilinx timing cell {cell_type!r} lacks input/output pins"
                )
            delay_name = {
                "MUXF9": "muxf9",
                "CARRY8": "carry8",
                "DSP48E2": "dsp48e2",
            }[cell_type]
            model["cells"][cell_type] = {
                "kind": "combinational",
                "inputs": inputs,
                "outputs": outputs,
                "delay_ns": delays[delay_name] + delays["sink_interconnect"],
            }
        elif cell_type in {"RAMB18E2", "RAMB36E2", "URAM288"}:
            clocks = [pin for pin in inputs if "CLK" in pin.upper()]
            if not clocks or not outputs:
                raise ValidationError(
                    f"Xilinx timing cell {cell_type!r} lacks clock/output pins"
                )
            primary_clock = clocks[0]
            controls = [
                pin for pin in inputs
                if pin != primary_clock and (
                    "CLK" in pin.upper()
                    or "RST" in pin.upper()
                    or pin.upper() == "SLEEP"
                )
            ]
            data_inputs = [
                pin for pin in inputs
                if pin != primary_clock and pin not in controls
            ]
            if not data_inputs:
                raise ValidationError(
                    f"Xilinx timing cell {cell_type!r} lacks data inputs"
                )
            prefix = "uram" if cell_type == "URAM288" else "bram"
            model["cells"][cell_type] = {
                "kind": "rising_edge_bank",
                "clock": primary_clock,
                "inputs": data_inputs,
                "controls": controls,
                "outputs": outputs,
                "setup_ns": delays[f"{prefix}_setup"] + delays["sink_interconnect"],
                "clock_to_q_ns": delays[f"{prefix}_clock_to_q"],
            }
        else:
            raise ValidationError(
                f"RapidWright pre-placement TimingDB does not cover {cell_type!r}"
            )
    model.update({
        "name": "rapidwright-ultrascaleplus-preplacement-v1",
        "family": "xcup",
        "source": {
            "provider": "rapidwright-delay-model-preplacement-v1",
            "qualification": "analytical_uncharacterized",
            "architecture_timing_db": str(timing_db_path),
            "rapidwright_revision": timing_db["source"]["revision"],
            "sink_interconnect_delay_ns": delays["sink_interconnect"],
            "hard_block_timing": "nonzero-conservative-scalar",
            "hold_analysis": "unavailable",
            "authoritative_setup": "Phase 7 routed timing plus global OpenSTA",
        },
    })
    if output_path is not None:
        write_json(output_path, model)
        load_timing_model(output_path)
    return model, instance_cell_types


def build_architecture_opensta_timing_model(
    ir: EmuIR,
    timing_db_path: Path,
    output_path: Optional[Path] = None,
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """Dispatch an Architecture TimingDB without guessing its provider."""

    schema = read_json(timing_db_path).get("schema")
    if schema == XILINX_PREPLACEMENT_TIMING_SCHEMA:
        return build_xilinx_preplacement_opensta_timing_model(
            ir, timing_db_path, output_path
        )
    raise ValidationError(
        f"unsupported Architecture TimingDB schema {schema!r}"
    )


def validate_timing_model_coverage(
    ir: EmuIR,
    model: Mapping[str, Any],
    instance_cell_types: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    used = ir.used_timing_types(instance_cell_types) if hasattr(ir, "used_timing_types") else sorted({
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


def _sealed_string_set_summary(values: Iterable[str]) -> Dict[str, Any]:
    """Describe a string set without copying a potentially huge list.

    Generated route-delay cells can create tens of thousands of distinct
    timing-model cell names.  The complete set remains available to the
    coverage checker above, but a persisted run report only needs a stable
    population count and content seal.
    """

    ordered = sorted(set(values))
    digest = hashlib.sha256()
    for value in ordered:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return {
        "count": len(ordered),
        "sha256": digest.hexdigest(),
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
    if hasattr(ir, "timing_connections") and hasattr(ir, "connection"):
        return _classify_indexed_through_net_timing_endpoints(
            ir, model, through_nets, instance_cell_types
        )

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
                (kind in {"rising_edge_ff", "falling_edge_ff"}
                 and pin == cell["output"])
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
                if pin in cell.get("controls", []):
                    continue
                if pin not in cell["inputs"]:
                    raise ValidationError(
                        "OpenSTA combinational sink pin is absent from the "
                        f"timing model: {cell_type}.{pin}"
                    )
                for output in cell.get("outputs", [cell.get("output")]):
                    for successor in output_nets.get((instance_id, output), ()):
                        forward_edges[net_id].add(successor)
                        reverse_edges[successor].add(net_id)
            elif kind in {"rising_edge_ff", "falling_edge_ff"}:
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
                elif pin != cell["clock"] and pin not in cell.get("controls", []):
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


def _classify_indexed_through_net_timing_endpoints(
    ir,
    model: Mapping[str, Any],
    through_nets: Sequence[str],
    instance_cell_types: Optional[Mapping[str, str]],
) -> Dict[str, Dict[str, Any]]:
    """Classify compact EmuIR using integer connectivity, not Python records."""

    from .sta import _scalar_pin

    requested: Dict[int, str] = {}
    for net in through_nets:
        row = ir.connection.execute(
            "SELECT seq FROM net_records WHERE id=?", (net,)
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"OpenSTA structural timing nets are absent from EmuIR: {[net]}"
            )
        requested[int(row[0])] = net
    if len(requested) != len(through_nets):
        raise ValidationError("OpenSTA structural timing nets are not unique")

    forward_edges: DefaultDict[int, set[int]] = defaultdict(set)
    reverse_edges: DefaultDict[int, set[int]] = defaultdict(set)
    timing_startpoints: set[int] = set()
    direct_timed: set[int] = set()
    direct_timed_counts: DefaultDict[int, int] = defaultdict(int)
    direct_timed_pins: DefaultDict[int, set[str]] = defaultdict(set)

    for instance, wires in ir.timing_connections():
        instance_id = instance["id"]
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
        pins = set(wires) | {
            (item["port"], item["bit"])
            for item in instance.get("constant_connections", [])
        }
        scalar_wires: Dict[str, int] = {}
        for (port, bit), net_seq in wires.items():
            scalar = _scalar_pin(port, bit, pins)
            previous = scalar_wires.setdefault(scalar, int(net_seq))
            if previous != int(net_seq):
                raise ValidationError(
                    "OpenSTA structural timing scalar pin is ambiguous"
                )
        kind = cell["kind"]
        if kind == "combinational":
            input_nets = []
            for pin in cell["inputs"]:
                if pin in cell.get("controls", []):
                    continue
                if pin in scalar_wires:
                    input_nets.append(scalar_wires[pin])
            output_nets = [
                scalar_wires[pin]
                for pin in cell.get("outputs", [cell.get("output")])
                if pin in scalar_wires
            ]
            for predecessor in input_nets:
                for successor in output_nets:
                    forward_edges[predecessor].add(successor)
                    reverse_edges[successor].add(predecessor)
        elif kind in {"rising_edge_ff", "falling_edge_ff"}:
            output = scalar_wires.get(cell["output"])
            if output is not None:
                timing_startpoints.add(output)
            data = scalar_wires.get(cell["data"])
            if data is not None:
                direct_timed.add(data)
                if data in requested:
                    direct_timed_counts[data] += 1
                    direct_timed_pins[data].add(
                        f"{instance_id}/{cell['data']}"
                    )
        elif kind == "rising_edge_bank":
            for pin in cell["outputs"]:
                output = scalar_wires.get(pin)
                if output is not None:
                    timing_startpoints.add(output)
            for pin in cell["inputs"]:
                data = scalar_wires.get(pin)
                if data is not None:
                    direct_timed.add(data)
                    if data in requested:
                        direct_timed_counts[data] += 1
                        direct_timed_pins[data].add(f"{instance_id}/{pin}")
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

    top_level_sink_counts: DefaultDict[int, int] = defaultdict(int)
    for row in ir.connection.execute(
        """SELECT net_seq, COUNT(*) FROM endpoint_records
             WHERE instance_seq IS NULL AND role=1 GROUP BY net_seq"""
    ):
        net_seq = int(row[0])
        if net_seq in requested:
            top_level_sink_counts[net_seq] = int(row[1])
    return {
        net: {
            "status": (
                "timed"
                if net_seq in reachable_from_startpoint
                and net_seq in reaches_timed
                else "no_timed_endpoint"
            ),
            "direct_timed_endpoints": direct_timed_counts[net_seq],
            "direct_timed_endpoint_pins": sorted(
                direct_timed_pins[net_seq]
            ),
            "direct_top_level_sinks": top_level_sink_counts[net_seq],
        }
        for net_seq, net in requested.items()
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


def check_opensta_export_completion(log: str, *, output_path: Path, clock_count: int) -> int:
    """Require the closed producer's exact completion record, not just exit 0."""
    records=re.findall(r'^EMUFLOW_OPENSTA_DATABASE status=pass clocks=(\d+) queried_paths=(\d+) emitted_paths=(\d+) output=(.+)\r?$',log,re.M)
    if len(records)!=1:
        raise EmuFlowError('OpenSTA export lacks one terminal completion record')
    clocks,queried,emitted,output=records[0]
    if int(clocks)!=clock_count or not 0<int(emitted)<=int(queried) or output.rstrip('\r')!=str(output_path):
        raise EmuFlowError('OpenSTA terminal completion identity/count differs')
    return int(emitted)


def _prepare_opensta_source_model(ir, timing_model_path, architecture_timing_db_path,
                                  clock_budget_policy):
    """Shared source primitive/clock-cone model for paths and launch tags."""
    instance_cell_types = None
    if architecture_timing_db_path is not None:
        model, instance_cell_types = build_architecture_opensta_timing_model(ir, architecture_timing_db_path)
    else:
        model = load_timing_model(timing_model_path)
    clock_logic = None
    if clock_budget_policy == 'engine-edge-v1':
        from .sta_clock_logic import specialize_clock_logic
        clock_logic = specialize_clock_logic(ir, model)
        instance_cell_types = ir.timing_types()
    coverage = validate_timing_model_coverage(ir, model, instance_cell_types)
    return model, instance_cell_types, clock_logic, coverage


def _write_opensta_source_files(ir, root, model, instance_cell_types, clock_map, timing_io):
    """Write one source context without running STA or enumerating paths."""
    write_mapped_verilog(ir, root / "mapped.v", timing_only=True,
                         timing_cell_types=instance_cell_types)
    (root / "timing.lib").write_text(render_opensta_liberty(model), encoding="utf-8")
    with (root / "clocks.tsv").open("w", encoding="utf-8") as stream:
        stream.write("clock_hex\tperiod_ns\n")
        for name, period in clock_map.items():
            stream.write(f"{name.encode().hex()}\t{period:.12g}\n")
    return _write_timing_io_map(ir, clock_map, timing_io, root / "timing-io.tsv")


def _run_opensta_path_database(
    ir_path: Path,
    output_path: Path,
    clocks: Optional[Mapping[str, float]] = None,
    timing_io: Optional[Mapping[str, Any]] = None,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
    executable: Optional[str] = None,
    max_paths: int = 200000,
    log_path: Optional[Path] = None,
    through_nets: Optional[Sequence[str]] = None,
    through_coverage_path: Optional[Path] = None,
    validate_output: bool = True,
    _return_database: bool = False,
    _stream_output: bool = False,
    _ir=None,
    clock_budget_policy: str = 'legacy-absolute-arrival-v1',
) -> Dict[str, Any]:
    if not isinstance(clock_budget_policy,str)or clock_budget_policy not in {'legacy-absolute-arrival-v1','engine-edge-v1'}:
        raise ValidationError('unsupported OpenSTA clock budget export policy')
    if clock_budget_policy=='engine-edge-v1':
        from .native_tools import require_native_schema
        require_native_schema('timing-path-store-v2')
    if max_paths <= 0:
        raise ValidationError("OpenSTA max_paths must be positive")
    if _stream_output and _return_database:
        raise ValidationError(
            "streamed OpenSTA output cannot return an inline database"
        )
    ir = _ir if _ir is not None else EmuIR.load(ir_path)
    through_net_ids = list(through_nets or [])
    if _stream_output and through_net_ids:
        raise ValidationError(
            "streamed OpenSTA output does not support directed through-net queries"
        )
    if (
        any(not isinstance(net, str) or not net for net in through_net_ids)
        or len(through_net_ids) != len(set(through_net_ids))
    ):
        raise ValidationError("OpenSTA through_nets must be unique net IDs")
    net_index = (
        {net["id"]: index for index, net in enumerate(ir.value["nets"])}
        if through_net_ids
        else {}
    )
    unknown_through_nets = sorted(set(through_net_ids) - set(net_index))
    if unknown_through_nets:
        raise ValidationError(
            f"OpenSTA through_nets are absent from EmuIR: {unknown_through_nets}"
        )
    model, instance_cell_types, clock_logic, coverage = _prepare_opensta_source_model(
        ir, timing_model_path, architecture_timing_db_path, clock_budget_policy)
    clock_map = _clock_map(ir, clocks)
    opensta = resolve_native_executable("sta", executable)
    engine = require_opensta_engine(opensta)
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
        timing_io_path = root / "timing-io.tsv"
        raw_path = root / "paths.tsv"
        through_path = root / "through-nets.tsv"
        through_endpoint_path = root / "through-endpoints.tsv"
        raw_through_coverage_path = root / "through-net-coverage.tsv"
        # OpenSTA's intentionally small Verilog reader does not accept net
        # declaration keywords on ports, attributes, or instance parameters.
        # Those constructs do not affect static timing connectivity.
        timing_io_summary = _write_opensta_source_files(
            ir, root, model, instance_cell_types, clock_map, timing_io)
        write_emuir_net_map(ir_path, net_map_path, _ir=ir)
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

        environment = os.environ.copy()
        environment.update(
            {
                "EMUFLOW_STA_LIBERTY": str(liberty_path),
                "EMUFLOW_STA_VERILOG": str(verilog_path),
                "EMUFLOW_STA_TOP": ir.value["design"]["top"],
                "EMUFLOW_STA_NET_MAP": str(net_map_path),
                "EMUFLOW_STA_CLOCKS": str(clock_path),
                "EMUFLOW_STA_TIMING_IO": str(timing_io_path),
                "EMUFLOW_STA_OUTPUT": str(raw_path),
                "EMUFLOW_STA_MAX_PATHS": str(max_paths),
                "EMUFLOW_STA_CLOCK_BUDGET_POLICY": clock_budget_policy,
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
        completed = run_with_bounded_output(
            [opensta, "-exit", str(OPENSTA_EXPORT_SCRIPT)],
            environment=environment,
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
        expected_records=(check_opensta_export_completion(completed.stdout,output_path=raw_path,clock_count=len(clock_map))
            if clock_budget_policy=='engine-edge-v1'else None)
        source_value = {
            # This owner records the search that selected the source members.
            # Replaying every member downstream does not prove coverage of
            # omitted endpoints or alternative paths after partitioning.
            'path_selection': ({'policy':'directed-through-net-qualification-v1',
                                'scope':'selected-source-paths'} if through_net_ids else
                               {'policy':'worst-per-endpoint-per-path-group-v1',
                                'scope':'selected-source-paths',
                                'max_paths_per_group':max_paths,'endpoint_path_count':1}),
            'clock_budget_policy':clock_budget_policy,
            'export_script_sha256':file_sha256(OPENSTA_EXPORT_SCRIPT),
            'design_context_script_sha256':file_sha256(OPENSTA_EXPORT_SCRIPT.with_name('design_context.tcl')),
            "timing_model": model["name"],
            "timing_model_qualification": model["source"]["qualification"],
            "engine": engine,
            "architecture_timing_db": (
                str(architecture_timing_db_path)
                if architecture_timing_db_path is not None else None
            ),
            "timing_io": timing_io_summary,
        }
        if clock_budget_policy=='engine-edge-v1':
            source_value['clock_budget_script_sha256']=file_sha256(OPENSTA_EXPORT_SCRIPT.with_name('clock_budget.tcl'))
            source_value['clock_logic_policy']=clock_logic['policy']
        if _stream_output:
            imported = import_sta_path_database_tsv_streaming(
                raw_path,
                ir_path,
                output_path,
                provider=OPENSTA_PROVIDER,
                source=source_value,
                _ir=ir,
                expected_records=expected_records,
            )
            database = read_json(output_path)
        else:
            imported = import_sta_path_database_tsv(
                raw_path,
                ir_path,
                output_path,
                provider=OPENSTA_PROVIDER,
                source=source_value,
                _ir=ir,
                _return_value=True,
                expected_records=expected_records,
            )
            database = imported.pop("_value")

        through_query_records = (
            _read_through_coverage_tsv(
                raw_through_coverage_path, through_net_ids
            )
            if through_net_ids
            else {}
        )

    checked = (
        validate_sta_path_database_path_value(output_path, database, ir)
        if validate_output
        else {"status": "deferred-to-managed-stage"}
    )
    covered_through_nets = []
    through_coverage: Dict[str, Any] | None = None
    if through_net_ids:
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
    result = {
        "status": "pass",
        "design": ir.value["design"]["name"],
        "provider": OPENSTA_PROVIDER,
        "timing_model": model["name"],
        "timing_model_qualification": model["source"]["qualification"],
        "engine": engine,
        "clocks": clock_map,
        "timing_io": timing_io_summary,
        "paths": imported["paths"],
        "path_selection": source_value['path_selection'],
        "max_paths": max_paths,
        "through_nets": through_net_ids,
        "covered_through_nets": covered_through_nets,
        "through_net_coverage": through_coverage,
        "path_limit_reached": imported["paths"] >= max_paths,
        "unique_path_nets": imported["unique_path_nets"],
        "used_cell_type_summary": _sealed_string_set_summary(
            coverage["used_cell_types"]
        ),
        "checker": checked,
        "output": str(output_path),
        "log": str(log_path) if log_path is not None else None,
    }
    if "path_qor" in imported:
        result["path_qor"] = imported["path_qor"]
    if _return_database:
        result["_database"] = database
    return result


def run_opensta_path_database(
    ir_path: Path, output_path: Path,
    clocks: Optional[Mapping[str, float]] = None,
    timing_io: Optional[Mapping[str, Any]] = None,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
    executable: Optional[str] = None, max_paths: int = 200000,
    log_path: Optional[Path] = None, through_nets: Optional[Sequence[str]] = None,
    through_coverage_path: Optional[Path] = None, validate_output: bool = True,
    _return_database: bool = False, _stream_output: bool = False,
    clock_budget_policy: str = 'legacy-absolute-arrival-v1',
) -> Dict[str, Any]:
    """Use compact streaming input for production Xilinx timing extraction."""
    from .emuir_storage import is_emuir_sqlite
    from .emuir_compact import is_compact_emuir
    from .indexed_timing import IndexedTimingView
    use_index = (_stream_output and not through_nets and is_emuir_sqlite(ir_path)
                 and is_compact_emuir(ir_path)
                 and (architecture_timing_db_path is None or
                      read_json(architecture_timing_db_path).get("schema") == XILINX_PREPLACEMENT_TIMING_SCHEMA))
    view = IndexedTimingView(ir_path) if use_index else None
    try:
        return _run_opensta_path_database(
            ir_path, output_path, clocks=clocks, timing_io=timing_io,
            timing_model_path=timing_model_path, architecture_timing_db_path=architecture_timing_db_path,
            executable=executable, max_paths=max_paths, log_path=log_path, through_nets=through_nets,
            through_coverage_path=through_coverage_path, validate_output=validate_output,
            _return_database=_return_database, _stream_output=_stream_output, _ir=view,
            clock_budget_policy=clock_budget_policy,
        )
    finally:
        if view is not None:
            view.close()
