"""OpenSTA setup analysis over RapidWright-routed UltraScale+ netlists."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import tempfile
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import file_sha256, read_json, write_json
from .ir import EmuIR
from .opensta import (
    DEFAULT_TIMING_MODEL,
    load_timing_model,
    require_opensta_engine,
    run_opensta_path_database,
)
from .native_tools import resolve_native_executable
from .resources import ResourceVector
from .sta import (
    STA_PATH_DATABASE_SCHEMA,
    STA_PATH_DATABASE_STREAM_SCHEMA,
    STA_PATH_DATABASE_INDEX_SCHEMA,
    sta_path_database_qor,
)
from .xilinx_timing import (
    XILINX_ROUTED_TIMING_SCHEMA,
    XILINX_ROUTED_TIMING_STREAM_SCHEMA,
    iter_xilinx_routed_timing_endpoints,
    validate_xilinx_routed_timing,
)
from .xilinx_netlist import is_xilinx_mapped_index
from .yosys import import_yosys_json, import_yosys_json_indexed


XILINX_ROUTED_OPENSTA_SCHEMA = "emuflow.xilinx-routed-opensta-summary/v1"
_PIN = re.compile(r"^(?P<port>.+?)(?:\[(?P<bit>[0-9]+)\])?$")
_RAM_TYPES = {"RAMB18E2", "RAMB36E2", "URAM288"}
_ROUTED_OPENSTA_WARNING = (
    "RapidWright route delays are exact per-sink setup arcs; hold timing "
    "and hard-block/clock intrinsic timing remain unqualified"
)


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _pin_identity(instance: str, pin: str) -> tuple[str, str, int]:
    match = _PIN.fullmatch(pin)
    if match is None:
        raise ValidationError(f"invalid routed logical pin {instance!r}/{pin!r}")
    return instance, match.group("port"), int(match.group("bit") or 0)


def _pin_sets(ir: EmuIR) -> Dict[str, Dict[str, set[tuple[str, int]]]]:
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
        for endpoint in instance.get("constant_connections", []):
            result[instance["id"]]["inputs"].add(
                (endpoint["port"], endpoint["bit"])
            )
    return result


def _scalar_pins(pins: set[tuple[str, int]]) -> list[str]:
    widths: Dict[str, int] = {}
    for port, bit in pins:
        widths[port] = max(widths.get(port, 0), bit + 1)
    return [
        port if widths[port] == 1 else f"{port}__{bit}"
        for port, bit in sorted(pins)
    ]


def _integer_parameter(
    instance: Mapping[str, Any], name: str, fallback: int = 0
) -> int:
    raw = instance.get("parameters", {}).get(name)
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return int(raw, 2) if all(bit in "01" for bit in raw) else int(raw)
        except ValueError:
            return fallback
    return fallback


def build_xilinx_routed_opensta_inputs(
    mapped_path: Path,
    timing_path: Path,
    *,
    timing_validation: Optional[Mapping[str, Any]] = None,
    _mapped_value: Optional[Mapping[str, Any]] = None,
    _timing_value: Optional[Mapping[str, Any]] = None,
    _mapped_sha256: Optional[str] = None,
    _timing_sha256: Optional[str] = None,
) -> tuple[EmuIR, Dict[str, Any], Dict[str, Any]]:
    """Insert one exact routed-delay arc per logical sink.

    The persisted routed timing database remains compact.  The expanded graph
    exists only in the OpenSTA staging directory and can be regenerated from
    the two sealed inputs.
    """

    # Standalone staging follows the same single-pass contract as the
    # production in-memory path: load each large input once, hash it once, and
    # hand the exact object to the independent validator and consumer.
    mapped_value = (
        read_json(mapped_path) if _mapped_value is None else _mapped_value
    )
    timing_value = (
        read_json(timing_path) if _timing_value is None else _timing_value
    )
    timing_sha256 = _timing_sha256 or _sha256(timing_path)
    mapped_sha256 = _mapped_sha256 or _sha256(mapped_path)
    delays: Dict[tuple[str, str, int], float] = {}

    def consume_endpoint(record: Mapping[str, Any]) -> None:
        sink = record["sink"]
        key = _pin_identity(sink["instance"], sink["pin"])
        if key in delays:
            raise ValidationError("RapidWright timing repeats a logical sink")
        delays[key] = float(record["route_delay_ns"])

    if timing_validation is None:
        timing_validation = validate_xilinx_routed_timing(
            timing_path,
            mapped_path=mapped_path,
            source_sha256={"mapped_sha256": mapped_sha256},
            _value=timing_value,
            _timing_sha256=timing_sha256,
            _endpoint_consumer=consume_endpoint,
        )
    else:
        for endpoint in iter_xilinx_routed_timing_endpoints(
            timing_path, timing_value
        ):
            consume_endpoint(endpoint)
    if (
        timing_validation.get("status") != "pass"
        or timing_validation.get("schema")
        != "emuflow.xilinx-routed-timing-validation/v1"
        or timing_validation.get("timing_sha256") != timing_sha256
    ):
        raise ValidationError("RapidWright routed timing validation seal is invalid")
    timing = timing_value
    if timing.get("schema") not in {
        XILINX_ROUTED_TIMING_SCHEMA, XILINX_ROUTED_TIMING_STREAM_SCHEMA,
    }:
        raise ValidationError("RapidWright routed timing input has the wrong schema")
    if timing.get("source", {}).get("mapped_sha256") != mapped_sha256:
        raise ValidationError("RapidWright routed timing mapped source seal is stale")
    source = import_yosys_json(mapped_path, _source_value=mapped_value)
    value = deepcopy(source.value)
    instances = list(value["instances"])
    nets = []
    bound = set()
    delay_types: Dict[float, str] = {}
    for net in value["nets"]:
        updated = deepcopy(net)
        updated_sinks = []
        for sink in net["sinks"]:
            if sink["instance"] is None:
                updated_sinks.append(deepcopy(sink))
                continue
            key = (sink["instance"], sink["port"], sink["bit"])
            delay = delays.get(key)
            if delay is None:
                updated_sinks.append(deepcopy(sink))
                continue
            bound.add(key)
            delay_type = delay_types.setdefault(
                delay, f"EMUFLOW_RW_ROUTE_DELAY_{len(delay_types):06d}"
            )
            index = len(bound) - 1
            # Keep synthetic identifiers flat.  OpenSTA treats ``/`` as its
            # hierarchy separator even when the Verilog reader accepted an
            # escaped identifier containing that character.  Route-delay
            # cells are not hierarchy, so encoding them as hierarchy is both
            # misleading and unsafe for later pin/path lookup.
            instance_id = f"__emuflow_rw_delay__{index:08d}"
            net_id = f"__emuflow_rw_delay_net__{index:08d}"
            instances.append(
                {
                    "id": instance_id,
                    "name": instance_id,
                    "type": delay_type,
                    "resources": ResourceVector().to_dict(),
                    "parameters": {},
                    "attributes": {"emuflow_route_delay_ns": delay},
                    "constant_connections": [],
                }
            )
            updated_sinks.append(
                {"instance": instance_id, "port": "A", "bit": 0}
            )
            nets.append(
                {
                    "id": net_id,
                    "name": net_id,
                    "aliases": [],
                    "bus_index": 0,
                    "drivers": [
                        {"instance": instance_id, "port": "Y", "bit": 0}
                    ],
                    "sinks": [deepcopy(sink)],
                    "fanout": 1,
                    "cut_class": net["cut_class"],
                }
            )
        updated["sinks"] = updated_sinks
        updated["fanout"] = len(updated_sinks)
        nets.append(updated)
    missing = sorted(set(delays) - bound)
    if missing:
        raise ValidationError(
            f"RapidWright timing has {len(missing)} unbound logical sinks"
        )
    negedge_ff_count = 0
    for instance in instances:
        cell_type = instance["type"]
        if (
            cell_type in {"FDCE", "FDPE", "FDRE", "FDSE"}
            and _integer_parameter(instance, "IS_C_INVERTED") == 1
        ):
            # This IR exists only to stage OpenSTA.  Keep the physical mapped
            # netlist unchanged, but select a distinct Liberty cell whose
            # active edge is falling so half-cycle paths remain visible.
            instance["type"] = f"{cell_type}__NEG"
            negedge_ff_count += 1
    value["instances"] = sorted(instances, key=lambda item: item["id"])
    value["nets"] = sorted(nets, key=lambda item: item["id"])
    value.setdefault("warnings", []).append(_ROUTED_OPENSTA_WARNING)
    routed_ir = EmuIR(value)

    model = deepcopy(load_timing_model(DEFAULT_TIMING_MODEL))
    coefficients = timing["qualification"]["logic_coefficients_ps"]
    conservative_lut_ns = max(float(value) for name, value in coefficients.items() if name.startswith("lut_")) / 1000.0
    for width in range(1, 7):
        model["cells"][f"LUT{width}"]["delay_ns"] = conservative_lut_ns
    for ff in ("FDCE", "FDPE", "FDRE", "FDSE"):
        model["cells"][ff]["clock_to_q_ns"] = (
            float(coefficients["ff_clock_to_q"]) / 1000.0
        )
        model["cells"][f"{ff}__NEG"] = deepcopy(model["cells"][ff])
        model["cells"][f"{ff}__NEG"]["kind"] = "falling_edge_ff"
    model["cells"]["LUT6_2"] = {
        "kind": "combinational",
        "inputs": [f"I{index}" for index in range(6)],
        "outputs": ["O5", "O6"],
        "delay_ns": conservative_lut_ns,
    }
    model["cells"]["CARRY8"] = {
        "kind": "combinational",
        "inputs": ["CI", "CI_TOP", *[f"DI__{index}" for index in range(8)], *[f"S__{index}" for index in range(8)]],
        "outputs": [*[f"CO__{index}" for index in range(8)], *[f"O__{index}" for index in range(8)]],
        "delay_ns": float(coefficients["carry_co"]) / 1000.0,
    }
    model["cells"].setdefault(
        "MUXF9",
        {"kind": "combinational", "inputs": ["I0", "I1", "S"], "output": "O", "delay_ns": model["cells"]["MUXF8"]["delay_ns"]},
    )
    pin_sets = _pin_sets(routed_ir)
    instances_by_type: Dict[str, list[Mapping[str, Any]]] = {}
    for instance in routed_ir.value["instances"]:
        instances_by_type.setdefault(instance["type"], []).append(instance)
    hard_blocks = []
    for cell_type, typed_instances in sorted(instances_by_type.items()):
        if cell_type in model["cells"]:
            continue
        # One Liberty cell declaration is shared by every instance of a
        # primitive.  Different hard-block instances routinely activate
        # different legal ports (for example ACOUT on one DSP and ACIN on the
        # next DSP in a cascade), so deriving the declaration from the first
        # instance silently drops ports from the remaining instances.  Form
        # the type-wide union while preserving each original port bit index.
        typed_inputs: set[tuple[str, int]] = set()
        typed_outputs: set[tuple[str, int]] = set()
        for instance in typed_instances:
            pins = pin_sets[instance["id"]]
            typed_inputs.update(pins["inputs"])
            typed_outputs.update(pins["outputs"])
        inputs = _scalar_pins(typed_inputs)
        outputs = _scalar_pins(typed_outputs)
        if cell_type in _RAM_TYPES:
            clocks = [pin for pin in inputs if "CLK" in pin.upper()]
            if not clocks or not outputs:
                raise ValidationError(f"hard block {cell_type} lacks clock/outputs")
            primary_clock = clocks[0]
            controls = [pin for pin in inputs if pin != primary_clock and ("CLK" in pin.upper() or "RST" in pin.upper() or pin.upper() == "SLEEP")]
            data_inputs = [pin for pin in inputs if pin != primary_clock and pin not in controls]
            if not data_inputs:
                raise ValidationError(f"hard block {cell_type} lacks timing inputs")
            model["cells"][cell_type] = {
                "kind": "rising_edge_bank",
                "clock": primary_clock,
                "inputs": data_inputs,
                "controls": controls,
                "outputs": outputs,
                "setup_ns": 0.0,
                "clock_to_q_ns": 0.0,
            }
            hard_blocks.append(cell_type)
            continue
        if cell_type == "DSP48E2":
            if not inputs or not outputs:
                raise ValidationError("DSP48E2 lacks timing inputs or outputs")
            model["cells"][cell_type] = {
                "kind": "combinational",
                "inputs": inputs,
                "outputs": outputs,
                # RapidWright lightweight timing does not publish a complete
                # DSP48E2 internal arc model. Keep the graph connected with a
                # deliberately conservative research bound and preserve the
                # unqualified hard-block marker in the summary.
                "delay_ns": 5.0,
            }
            hard_blocks.append(cell_type)
            continue
        if cell_type.startswith("EMUFLOW_RW_ROUTE_DELAY_"):
            instance_delays = {
                float(instance["attributes"]["emuflow_route_delay_ns"])
                for instance in typed_instances
            }
            if len(instance_delays) != 1:
                raise ValidationError(
                    "shared routed-delay timing cell type has unequal delays"
                )
            delay = instance_delays.pop()
            model["cells"][cell_type] = {
                "kind": "combinational", "inputs": ["A"],
                "output": "Y", "delay_ns": delay,
            }
            continue
        raise ValidationError(
            f"RapidWright OpenSTA model does not cover {cell_type!r}"
        )
    model.update(
        {
            "name": "rapidwright-ultrascaleplus-routed-v1",
            "family": "xcup",
            "source": {
                "provider": "rapidwright-lightweight+routed-opensta-v1",
                "qualification": "analytical_uncharacterized",
                "route_delays": "rapidwright-lightweight-exact-per-sink",
                "logic_delays": "rapidwright-lightweight-conservative-scalar",
                "ff_setup": "analytical-uncharacterized",
                "hard_block_timing": "zero-delay-sequential-surrogate-unqualified",
                "hold_analysis": "unavailable",
            },
        }
    )
    metadata = {
        "logical_route_endpoints": len(delays),
        "inserted_route_delay_cells": len(bound),
        "unique_route_delay_cells": len(delay_types),
        "hard_block_types_unqualified": sorted(set(hard_blocks)),
        "falling_edge_ff_instances": negedge_ff_count,
    }
    return routed_ir, model, metadata


def build_xilinx_routed_opensta_indexed_inputs(
    mapped_path: Path,
    timing_path: Path,
    output_path: Path,
    *,
    clocks: Mapping[str, float],
    timing_validation: Optional[Mapping[str, Any]] = None,
    timing_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
    timing_sha256: Optional[str] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Build routed OpenSTA input without a graph-sized Python EmuIR copy."""

    timing_value = read_json(timing_path) if timing_value is None else timing_value
    timing_sha256 = timing_sha256 or _sha256(timing_path)
    mapped_sha256 = mapped_sha256 or _sha256(mapped_path)
    pending: Dict[tuple[str, str, int], tuple[float, int, str]] = {}
    delay_types: Dict[float, str] = {}

    def consume_endpoint(record: Mapping[str, Any]) -> None:
        sink = record["sink"]
        key = _pin_identity(sink["instance"], sink["pin"])
        if key in pending:
            raise ValidationError("RapidWright timing repeats a logical sink")
        delay = float(record["route_delay_ns"])
        timing_type = delay_types.setdefault(
            delay, f"EMUFLOW_RW_ROUTE_DELAY_{len(delay_types):06d}"
        )
        pending[key] = (delay, len(pending), timing_type)

    if timing_validation is None:
        timing_validation = validate_xilinx_routed_timing(
            timing_path,
            mapped_path=mapped_path,
            source_sha256={"mapped_sha256": mapped_sha256},
            _value=timing_value,
            _timing_sha256=timing_sha256,
            _endpoint_consumer=consume_endpoint,
        )
    else:
        for endpoint in iter_xilinx_routed_timing_endpoints(
            timing_path, timing_value
        ):
            consume_endpoint(endpoint)
    if (
        timing_validation.get("status") != "pass"
        or timing_validation.get("schema")
        != "emuflow.xilinx-routed-timing-validation/v1"
        or timing_validation.get("timing_sha256") != timing_sha256
    ):
        raise ValidationError("RapidWright routed timing validation seal is invalid")
    timing = timing_value
    if timing.get("schema") not in {
        XILINX_ROUTED_TIMING_SCHEMA, XILINX_ROUTED_TIMING_STREAM_SCHEMA,
    }:
        raise ValidationError("RapidWright routed timing input has the wrong schema")
    if timing.get("source", {}).get("mapped_sha256") != mapped_sha256:
        raise ValidationError("RapidWright routed timing mapped source seal is stale")

    transform_state: Dict[str, Any] = {}

    def stage_route_delays(
        connection: Any,
        raw_ports: Mapping[str, Any],
        _input_path: Path,
        _top: str,
    ) -> Dict[str, Any]:
        maximum_bit = int(
            connection.execute(
                "SELECT COALESCE(MAX(yosys_bit),1) FROM staging_names"
            ).fetchone()[0]
        )
        for port in raw_ports.values():
            if isinstance(port, Mapping):
                for bit in port.get("bits", []):
                    if isinstance(bit, int):
                        maximum_bit = max(maximum_bit, bit)
        next_bit = maximum_bit + 1
        connection.execute(
            "ALTER TABLE staging_instances ADD COLUMN "
            "emuflow_original INTEGER NOT NULL DEFAULT 1"
        )
        pins: Dict[str, Dict[str, set[tuple[str, int]]]] = defaultdict(
            lambda: {"inputs": set(), "outputs": set()}
        )
        falling_edge_ff_instances = 0
        inserted = 0
        rows = connection.execute(
            """
            SELECT id,type,record_json
              FROM staging_instances
             WHERE emuflow_original=1
             ORDER BY id
            """,
        )
        for row in rows:
            name = str(row["id"])
            cell = json.loads(row["record_json"])
            if not isinstance(cell, dict):
                raise ValidationError(f"mapped cell {name!r} is invalid")
            cell_type = str(cell.get("type", row["type"]))
            directions = cell.get("port_directions", {})
            connections = cell.get("connections", {})
            if not isinstance(directions, dict) or not isinstance(
                connections, dict
            ):
                raise ValidationError(f"mapped cell {name!r} lacks port metadata")
            if (
                cell_type in {"FDCE", "FDPE", "FDRE", "FDSE"}
                and _integer_parameter(cell, "IS_C_INVERTED") == 1
            ):
                cell_type = f"{cell_type}__NEG"
                cell["type"] = cell_type
                falling_edge_ff_instances += 1
            for port, bits in connections.items():
                if not isinstance(bits, list):
                    raise ValidationError(
                        f"mapped cell {name!r} port {port!r} is invalid"
                    )
                direction = directions.get(port)
                for bit_index in range(len(bits)):
                    if direction in {"input", "inout"}:
                        pins[cell_type]["inputs"].add((str(port), bit_index))
                    if direction in {"output", "inout"}:
                        pins[cell_type]["outputs"].add((str(port), bit_index))
                    key = (name, str(port), bit_index)
                    delay_record = pending.pop(key, None)
                    if delay_record is None:
                        continue
                    if direction not in {"input", "inout"}:
                        raise ValidationError(
                            "RapidWright route delay is not bound to an input pin"
                        )
                    original_bit = bits[bit_index]
                    if not isinstance(original_bit, int):
                        raise ValidationError(
                            "RapidWright route delay is bound to a constant pin"
                        )
                    delay, index, timing_type = delay_record
                    routed_bit = next_bit
                    next_bit += 1
                    bits[bit_index] = routed_bit
                    delay_name = f"__emuflow_rw_delay__{index:08d}"
                    delay_cell = {
                        "type": timing_type,
                        "parameters": {},
                        "attributes": {"emuflow_route_delay_ns": delay},
                        "port_directions": {"A": "input", "Y": "output"},
                        "connections": {"A": [original_bit], "Y": [routed_bit]},
                    }
                    connection.execute(
                        "INSERT INTO staging_instances"
                        "(id,type,ff,bram,record_json,emuflow_original) "
                        "VALUES (?,?,?,?,?,0)",
                        (
                            delay_name, timing_type, 0, 0,
                            json.dumps(
                                delay_cell, ensure_ascii=False,
                                separators=(",", ":"), sort_keys=True,
                            ),
                        ),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO staging_names VALUES (?,?,?,?,?,?)",
                        (
                            routed_bit, 1,
                            len(f"__emuflow_rw_delay_net__{index:08d}"),
                            f"__emuflow_rw_delay_net__{index:08d}", 0, 1,
                        ),
                    )
                    pins[timing_type]["inputs"].add(("A", 0))
                    pins[timing_type]["outputs"].add(("Y", 0))
                    inserted += 1
            connection.execute(
                "UPDATE staging_instances SET type=?,record_json=? WHERE id=?",
                (
                    cell_type,
                    json.dumps(
                        cell, ensure_ascii=False,
                        separators=(",", ":"), sort_keys=True,
                    ),
                    name,
                ),
            )
        if pending:
            examples = ", ".join(repr(key) for key in sorted(pending)[:3])
            raise ValidationError(
                f"RapidWright timing has {len(pending)} unbound logical sinks: "
                + examples
            )
        result = {
            "inserted_route_delay_cells": inserted,
            "falling_edge_ff_instances": falling_edge_ff_instances,
            "pin_sets": {
                cell_type: {
                    role: [list(pin) for pin in sorted(records)]
                    for role, records in roles.items()
                }
                for cell_type, roles in sorted(pins.items())
            },
        }
        transform_state.update(result)
        return result

    transform_report = import_yosys_json_indexed(
        mapped_path,
        output_path,
        top=str(timing["top"]),
        clocks=tuple(clocks),
        _source_index_path=(
            mapped_path if is_xilinx_mapped_index(mapped_path) else None
        ),
        staging_transform=stage_route_delays,
    )
    if transform_report is None or transform_report != transform_state:
        raise ValidationError("indexed RapidWright route-delay staging failed")
    # Preserve the historical timing-only cell accounting and qualification
    # warning without materializing the logical EmuIR graph.  ``cut_class``
    # may be recomputed around the inserted delay cell; OpenSTA consumes the
    # connectivity and timing model, not this partitioning-only annotation.
    connection = sqlite3.connect(output_path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        zero_resources = json.dumps(
            ResourceVector().to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            "INSERT OR IGNORE INTO payloads(value) VALUES (?)",
            (zero_resources,),
        )
        zero_seq = int(
            connection.execute(
                "SELECT seq FROM payloads WHERE value=?", (zero_resources,)
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE cell_records SET resources_seq=? "
            "WHERE type LIKE 'EMUFLOW_RW_ROUTE_DELAY_%'",
            (zero_seq,),
        )
        delay_nets = [
            int(row[0])
            for row in connection.execute(
                "SELECT seq FROM net_records "
                "WHERE id LIKE '__emuflow_rw_delay_net__%'"
            )
        ]
        if delay_nets:
            placeholders = ",".join("?" for _ in delay_nets)
            connection.execute(
                f"DELETE FROM alias_records WHERE net_seq IN ({placeholders})",
                delay_nets,
            )
            connection.execute(
                f"UPDATE net_records SET yosys_bit=NULL "
                f"WHERE seq IN ({placeholders})",
                delay_nets,
            )
        warning_row = connection.execute(
            "SELECT value FROM metadata WHERE key='warnings_json'"
        ).fetchone()
        warnings = json.loads(warning_row[0]) if warning_row else []
        if _ROUTED_OPENSTA_WARNING not in warnings:
            warnings.append(_ROUTED_OPENSTA_WARNING)
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='warnings_json'",
            (json.dumps(warnings, separators=(",", ":"), sort_keys=True),),
        )
        connection.commit()
    finally:
        connection.close()

    model = deepcopy(load_timing_model(DEFAULT_TIMING_MODEL))
    coefficients = timing["qualification"]["logic_coefficients_ps"]
    conservative_lut_ns = max(
        float(value) for name, value in coefficients.items()
        if name.startswith("lut_")
    ) / 1000.0
    for width in range(1, 7):
        model["cells"][f"LUT{width}"]["delay_ns"] = conservative_lut_ns
    for ff in ("FDCE", "FDPE", "FDRE", "FDSE"):
        model["cells"][ff]["clock_to_q_ns"] = (
            float(coefficients["ff_clock_to_q"]) / 1000.0
        )
        model["cells"][f"{ff}__NEG"] = deepcopy(model["cells"][ff])
        model["cells"][f"{ff}__NEG"]["kind"] = "falling_edge_ff"
    model["cells"]["LUT6_2"] = {
        "kind": "combinational",
        "inputs": [f"I{index}" for index in range(6)],
        "outputs": ["O5", "O6"],
        "delay_ns": conservative_lut_ns,
    }
    model["cells"]["CARRY8"] = {
        "kind": "combinational",
        "inputs": [
            "CI", "CI_TOP", *[f"DI__{index}" for index in range(8)],
            *[f"S__{index}" for index in range(8)],
        ],
        "outputs": [
            *[f"CO__{index}" for index in range(8)],
            *[f"O__{index}" for index in range(8)],
        ],
        "delay_ns": float(coefficients["carry_co"]) / 1000.0,
    }
    model["cells"].setdefault(
        "MUXF9",
        {
            "kind": "combinational", "inputs": ["I0", "I1", "S"],
            "output": "O", "delay_ns": model["cells"]["MUXF8"]["delay_ns"],
        },
    )
    hard_blocks = []
    for cell_type, records in transform_state["pin_sets"].items():
        if cell_type in model["cells"]:
            continue
        inputs = _scalar_pins({tuple(pin) for pin in records["inputs"]})
        outputs = _scalar_pins({tuple(pin) for pin in records["outputs"]})
        if cell_type in _RAM_TYPES:
            clock_pins = [pin for pin in inputs if "CLK" in pin.upper()]
            if not clock_pins or not outputs:
                raise ValidationError(f"hard block {cell_type} lacks clock/outputs")
            primary_clock = clock_pins[0]
            controls = [
                pin for pin in inputs
                if pin != primary_clock and (
                    "CLK" in pin.upper() or "RST" in pin.upper()
                    or pin.upper() == "SLEEP"
                )
            ]
            data_inputs = [
                pin for pin in inputs
                if pin != primary_clock and pin not in controls
            ]
            if not data_inputs:
                raise ValidationError(f"hard block {cell_type} lacks timing inputs")
            model["cells"][cell_type] = {
                "kind": "rising_edge_bank", "clock": primary_clock,
                "inputs": data_inputs, "controls": controls,
                "outputs": outputs, "setup_ns": 0.0, "clock_to_q_ns": 0.0,
            }
            hard_blocks.append(cell_type)
        elif cell_type == "DSP48E2":
            if not inputs or not outputs:
                raise ValidationError("DSP48E2 lacks timing inputs or outputs")
            model["cells"][cell_type] = {
                "kind": "combinational", "inputs": inputs,
                "outputs": outputs, "delay_ns": 5.0,
            }
            hard_blocks.append(cell_type)
        elif cell_type.startswith("EMUFLOW_RW_ROUTE_DELAY_"):
            delay = next(
                value for value, name in delay_types.items()
                if name == cell_type
            )
            model["cells"][cell_type] = {
                "kind": "combinational", "inputs": ["A"],
                "output": "Y", "delay_ns": delay,
            }
        else:
            raise ValidationError(
                f"RapidWright OpenSTA model does not cover {cell_type!r}"
            )
    model.update({
        "name": "rapidwright-ultrascaleplus-routed-v1",
        "family": "xcup",
        "source": {
            "provider": "rapidwright-lightweight+routed-opensta-v1",
            "qualification": "analytical_uncharacterized",
            "route_delays": "rapidwright-lightweight-exact-per-sink",
            "logic_delays": "rapidwright-lightweight-conservative-scalar",
            "ff_setup": "analytical-uncharacterized",
            "hard_block_timing": "zero-delay-sequential-surrogate-unqualified",
            "hold_analysis": "unavailable",
        },
    })
    metadata = {
        "logical_route_endpoints": int(timing["summary"]["logical_endpoints"]),
        "inserted_route_delay_cells": transform_state[
            "inserted_route_delay_cells"
        ],
        "unique_route_delay_cells": len(delay_types),
        "hard_block_types_unqualified": sorted(set(hard_blocks)),
        "falling_edge_ff_instances": transform_state[
            "falling_edge_ff_instances"
        ],
        "representation": "compact-emuir-sqlite",
    }
    return model, metadata


def _qor(database: Mapping[str, Any]) -> Dict[str, Any]:
    slacks = [float(path["slack_ns"]) for path in database["paths"]]
    if not slacks:
        raise ValidationError("RapidWright OpenSTA produced no timing paths")
    return {
        "wns_ns": min(slacks),
        "tns_ns": math.fsum(value for value in slacks if value < 0.0),
        "failing_endpoints": sum(value < 0.0 for value in slacks),
        "timed_endpoints": len(slacks),
    }


def _qor_path(path: Path) -> Dict[str, Any]:
    database = read_json(path)
    if database.get("schema") in {
        STA_PATH_DATABASE_SCHEMA,
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
    }:
        return sta_path_database_qor(path, database)
    # Preserve the minimal schema-less fixture accepted by historical tests.
    return _qor(database)


def run_xilinx_routed_opensta(
    mapped_path: Path,
    timing_path: Path,
    output_path: Path,
    summary_path: Path,
    *,
    clocks: Mapping[str, float],
    executable: Optional[str] = None,
    max_paths: int = 200000,
    log_path: Optional[Path] = None,
    timing_validation: Optional[Mapping[str, Any]] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    timing_value: Optional[Mapping[str, Any]] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    # Engine availability is a constant-time prerequisite.  Check it before
    # expanding hundreds of thousands of routed endpoints into the temporary
    # OpenSTA graph so a missing runtime dependency fails immediately rather
    # than after minutes of staging work.
    opensta = resolve_native_executable("sta", executable)
    require_opensta_engine(opensta)
    timing_value = (
        read_json(timing_path) if timing_value is None else timing_value
    )
    input_sha256 = (
        {
            "mapped_sha256": _sha256(mapped_path),
            "routed_timing_sha256": _sha256(timing_path),
        }
        if source_sha256 is None else dict(source_sha256)
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-rw-opensta-") as temporary:
        root = Path(temporary)
        ir_path = root / "routed.emuir.sqlite"
        model_path = root / "routed-timing-model.json"
        model, metadata = build_xilinx_routed_opensta_indexed_inputs(
            mapped_path,
            timing_path,
            ir_path,
            clocks=clocks,
            timing_validation=timing_validation,
            timing_value=timing_value,
            mapped_sha256=input_sha256.get("mapped_sha256"),
            timing_sha256=input_sha256.get("routed_timing_sha256"),
        )
        write_json(model_path, model, compact=True)
        report = run_opensta_path_database(
            ir_path, output_path, clocks=clocks,
            timing_model_path=model_path, executable=opensta,
            max_paths=max_paths, log_path=log_path,
            _stream_output=True,
        )
    if report["path_limit_reached"]:
        raise ValidationError(
            "RapidWright OpenSTA path limit was reached; global TNS is incomplete"
        )
    path_database_sha256 = _sha256(output_path)
    summary = {
        "schema": XILINX_ROUTED_OPENSTA_SCHEMA,
        "status": "pass",
        "authority": "opensta",
        "qualification": model["source"],
        "source": {
            **input_sha256,
            "timing_path_database_sha256": path_database_sha256,
        },
        "clocks": dict(sorted(clocks.items())),
        "staging": metadata,
        "qor": report["path_qor"],
        "opensta": report,
    }
    write_json(summary_path, summary, compact=True)
    return validate_xilinx_routed_opensta_summary(
        summary_path, output_path=output_path,
        mapped_path=mapped_path, timing_path=timing_path,
        source_sha256={
            **input_sha256,
            "timing_path_database_sha256": path_database_sha256,
        },
        _qor_value=report["path_qor"],
    )


def validate_xilinx_routed_opensta_summary(
    summary_path: Path,
    *,
    output_path: Path,
    mapped_path: Optional[Path] = None,
    timing_path: Optional[Path] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    _database: Optional[Mapping[str, Any]] = None,
    _qor_value: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    value = read_json(summary_path)
    if value.get("schema") != XILINX_ROUTED_OPENSTA_SCHEMA or value.get("status") != "pass":
        raise ValidationError("RapidWright OpenSTA summary header is invalid")
    expected = {
        "timing_path_database_sha256": output_path,
        "mapped_sha256": mapped_path,
        "routed_timing_sha256": timing_path,
    }
    expected_sha256 = (
        {field: _sha256(path) for field, path in expected.items()
         if path is not None}
        if source_sha256 is None else dict(source_sha256)
    )
    for field, path in expected.items():
        digest = value.get("source", {}).get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"RapidWright OpenSTA source.{field} is invalid")
        if path is not None and digest != expected_sha256.get(field):
            raise ValidationError(f"RapidWright OpenSTA source.{field} disagrees")
    recomputed = (
        dict(_qor_value)
        if _qor_value is not None
        else (
            _qor(_database)
            if _database is not None
            else _qor_path(output_path)
        )
    )
    reported = value.get("qor")
    if not isinstance(reported, dict):
        raise ValidationError("RapidWright OpenSTA QoR is invalid")
    for field in ("wns_ns", "tns_ns"):
        if not math.isclose(float(reported.get(field, math.nan)), recomputed[field], rel_tol=1e-9, abs_tol=1e-9):
            raise ValidationError(f"RapidWright OpenSTA {field} disagrees")
    for field in ("failing_endpoints", "timed_endpoints"):
        if reported.get(field) != recomputed[field]:
            raise ValidationError(f"RapidWright OpenSTA {field} disagrees")
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-routed-opensta-validation/v1",
        **recomputed,
        "summary_sha256": _sha256(summary_path),
    }
