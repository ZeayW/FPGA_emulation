"""OpenSTA setup analysis over RapidWright-routed UltraScale+ netlists."""

from __future__ import annotations

import hashlib
import math
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .opensta import (
    DEFAULT_TIMING_MODEL,
    load_timing_model,
    run_opensta_path_database,
)
from .resources import ResourceVector
from .sta import validate_sta_path_database
from .xilinx_timing import (
    XILINX_ROUTED_TIMING_SCHEMA,
    validate_xilinx_routed_timing,
)
from .yosys import import_yosys_json


XILINX_ROUTED_OPENSTA_SCHEMA = "emuflow.xilinx-routed-opensta-summary/v1"
_PIN = re.compile(r"^(?P<port>.+?)(?:\[(?P<bit>[0-9]+)\])?$")
_RAM_TYPES = {"RAMB18E2", "RAMB36E2", "URAM288"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def build_xilinx_routed_opensta_inputs(
    mapped_path: Path,
    timing_path: Path,
) -> tuple[EmuIR, Dict[str, Any], Dict[str, Any]]:
    """Insert one exact routed-delay arc per logical sink.

    The persisted routed timing database remains compact.  The expanded graph
    exists only in the OpenSTA staging directory and can be regenerated from
    the two sealed inputs.
    """

    validate_xilinx_routed_timing(timing_path, mapped_path=mapped_path)
    timing = read_json(timing_path)
    if timing.get("schema") != XILINX_ROUTED_TIMING_SCHEMA:
        raise ValidationError("RapidWright routed timing input has the wrong schema")
    source = import_yosys_json(mapped_path)
    delays: Dict[tuple[str, str, int], float] = {}
    for record in timing["endpoints"]:
        sink = record["sink"]
        key = _pin_identity(sink["instance"], sink["pin"])
        if key in delays:
            raise ValidationError("RapidWright timing repeats a logical sink")
        delays[key] = float(record["route_delay_ns"])

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
    value["instances"] = sorted(instances, key=lambda item: item["id"])
    value["nets"] = sorted(nets, key=lambda item: item["id"])
    value.setdefault("warnings", []).append(
        "RapidWright route delays are exact per-sink setup arcs; hold timing "
        "and hard-block/clock intrinsic timing remain unqualified"
    )
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
            delays = {
                float(instance["attributes"]["emuflow_route_delay_ns"])
                for instance in typed_instances
            }
            if len(delays) != 1:
                raise ValidationError(
                    "shared routed-delay timing cell type has unequal delays"
                )
            delay = delays.pop()
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
    }
    return routed_ir, model, metadata


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
) -> Dict[str, Any]:
    routed_ir, model, metadata = build_xilinx_routed_opensta_inputs(
        mapped_path, timing_path
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-rw-opensta-") as temporary:
        root = Path(temporary)
        ir_path = root / "routed.emuir.json"
        model_path = root / "routed-timing-model.json"
        write_json(ir_path, routed_ir.value, compact=True)
        write_json(model_path, model, compact=True)
        report = run_opensta_path_database(
            ir_path, output_path, clocks=clocks,
            timing_model_path=model_path, executable=executable,
            max_paths=max_paths, log_path=log_path,
        )
        validate_sta_path_database(output_path, ir_path)
    if report["path_limit_reached"]:
        raise ValidationError(
            "RapidWright OpenSTA path limit was reached; global TNS is incomplete"
        )
    database = read_json(output_path)
    summary = {
        "schema": XILINX_ROUTED_OPENSTA_SCHEMA,
        "status": "pass",
        "authority": "opensta",
        "qualification": model["source"],
        "source": {
            "mapped_sha256": _sha256(mapped_path),
            "routed_timing_sha256": _sha256(timing_path),
            "timing_path_database_sha256": _sha256(output_path),
        },
        "clocks": dict(sorted(clocks.items())),
        "staging": metadata,
        "qor": _qor(database),
        "opensta": report,
    }
    write_json(summary_path, summary, compact=True)
    return validate_xilinx_routed_opensta_summary(
        summary_path, output_path=output_path,
        mapped_path=mapped_path, timing_path=timing_path,
    )


def validate_xilinx_routed_opensta_summary(
    summary_path: Path,
    *,
    output_path: Path,
    mapped_path: Optional[Path] = None,
    timing_path: Optional[Path] = None,
) -> Dict[str, Any]:
    value = read_json(summary_path)
    if value.get("schema") != XILINX_ROUTED_OPENSTA_SCHEMA or value.get("status") != "pass":
        raise ValidationError("RapidWright OpenSTA summary header is invalid")
    expected = {
        "timing_path_database_sha256": output_path,
        "mapped_sha256": mapped_path,
        "routed_timing_sha256": timing_path,
    }
    for field, path in expected.items():
        digest = value.get("source", {}).get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"RapidWright OpenSTA source.{field} is invalid")
        if path is not None and digest != _sha256(path):
            raise ValidationError(f"RapidWright OpenSTA source.{field} disagrees")
    database = read_json(output_path)
    recomputed = _qor(database)
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
