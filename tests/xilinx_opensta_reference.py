"""Small independent graph oracle; never a production fallback."""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.errors import ValidationError
from emuflow.store_publication import (
    StoreSource,
    bind_store_source,
    require_store_sources_unchanged,
)
from emuflow.io import file_sha256, read_json, write_json
from emuflow.ir import EmuIR
from emuflow.opensta import (
    DEFAULT_TIMING_MODEL,
    load_timing_model,
    require_opensta_engine,
    run_opensta_path_database,
)
from emuflow.native_tools import resolve_native_executable
from emuflow.resources import ResourceVector
from emuflow.sta import (
    STA_PATH_DATABASE_SCHEMA,
    STA_PATH_DATABASE_STREAM_SCHEMA,
    STA_PATH_DATABASE_INDEX_SCHEMA,
    sta_path_database_qor,
)
from emuflow.xilinx_timing import (
    XILINX_ROUTED_TIMING_SCHEMA,
    XILINX_ROUTED_TIMING_STREAM_SCHEMA,
    iter_xilinx_routed_timing_endpoints,
    validate_xilinx_routed_timing,
)
from emuflow.xilinx_netlist import is_xilinx_mapped_index
from emuflow.yosys import import_yosys_json, import_yosys_json_indexed


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


