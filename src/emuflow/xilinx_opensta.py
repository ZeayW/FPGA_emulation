"""OpenSTA setup analysis over RapidWright-routed UltraScale+ netlists."""

from __future__ import annotations

import math
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .store_publication import (
    StoreSource,
    bind_store_source,
    require_store_sources_unchanged,
)
from .io import file_sha256, read_json, write_json
from .opensta import (
    DEFAULT_TIMING_MODEL,
    load_timing_model,
    require_opensta_engine,
    run_opensta_path_database,
)
from .native_tools import resolve_native_executable
from .sta import (
    STA_PATH_DATABASE_SCHEMA,
    STA_PATH_DATABASE_STREAM_SCHEMA,
    STA_PATH_DATABASE_INDEX_SCHEMA,
    sta_path_database_qor,
)
from .xilinx_timing import XILINX_ROUTED_TIMING_STREAM_SCHEMA

from .xilinx_netlist import is_xilinx_mapped_index


XILINX_ROUTED_OPENSTA_SCHEMA = "emuflow.xilinx-routed-opensta-summary/v1"
_RAM_TYPES = {"RAMB18E2", "RAMB36E2", "URAM288"}


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _scalar_pins(pins: set[tuple[str, int]]) -> list[str]:
    widths: Dict[str, int] = {}
    for port, bit in pins:
        widths[port] = max(widths.get(port, 0), bit + 1)
    return [
        port if widths[port] == 1 else f"{port}__{bit}"
        for port, bit in sorted(pins)
    ]


def _bind_routed_opensta_inputs(mapped_path, timing_path, timing_value=None):
    mapped_source = bind_store_source(mapped_path, "yosys-source-document-v1")
    timing_source = bind_store_source(timing_path, "sta-source-document-v1")
    if is_xilinx_mapped_index(mapped_path):
        mapped_source = StoreSource(
            mapped_source.path, "xilinx-mapped-index-v2", mapped_source.generation
        )
    timing_value = read_json(timing_path) if timing_value is None else timing_value
    require_store_sources_unchanged((mapped_source, timing_source))
    sources = (mapped_source, timing_source)
    if timing_value.get("schema") == XILINX_ROUTED_TIMING_STREAM_SCHEMA:
        from .routed_endpoints_native import bind_routed_timing_sources
        sources = (mapped_source, *bind_routed_timing_sources(timing_path, timing_value))
    require_store_sources_unchanged(sources)
    return sources, timing_value


def _routed_base_model(timing, pin_sets):
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
    for cell_type, records in pin_sets.items():
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
    return model, hard_blocks

def build_xilinx_routed_opensta_indexed_inputs(
    mapped_path: Path, timing_path: Path, output_path: Path, *,
    clocks: Mapping[str, float], timing_validation=None, timing_value=None,
    mapped_sha256=None, timing_sha256=None, _source_bindings=None,
):
    from .routed_opensta_native import stage_routed_opensta
    return stage_routed_opensta(mapped_path, timing_path, output_path, clocks=clocks,
        timing_validation=timing_validation, timing_value=timing_value,
        mapped_sha256=mapped_sha256, timing_sha256=timing_sha256,
        source_bindings=_source_bindings)


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
    source_bindings, timing_value = _bind_routed_opensta_inputs(
        mapped_path, timing_path, timing_value
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
        model, metadata = build_xilinx_routed_opensta_indexed_inputs(
            mapped_path,
            timing_path,
            ir_path,
            clocks=clocks,
            timing_validation=timing_validation,
            timing_value=timing_value,
            mapped_sha256=input_sha256.get("mapped_sha256"),
            timing_sha256=input_sha256.get("routed_timing_sha256"),
            _source_bindings=source_bindings,
        )
        report = run_opensta_path_database(
            ir_path, output_path, clocks=clocks,
            _timing_model=model, executable=opensta,
            max_paths=max_paths, log_path=log_path,
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
