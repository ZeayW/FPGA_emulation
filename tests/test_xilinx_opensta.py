import hashlib
import json
import tempfile
from pathlib import Path

from emuflow.xilinx_opensta import (
    build_xilinx_routed_opensta_inputs,
    validate_xilinx_routed_opensta_summary,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_routed_opensta_staging_inserts_one_exact_delay_per_sink():
    mapped = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": {
                    "clk": {"direction": "input", "bits": [2]},
                    "a": {"direction": "input", "bits": [3]},
                    "q": {"direction": "output", "bits": [5]},
                },
                "cells": {
                    "lut": {
                        "type": "LUT1",
                        "parameters": {"INIT": "10"},
                        "port_directions": {"I0": "input", "O": "output"},
                        "connections": {"I0": [3], "O": [4]},
                    },
                    "ff": {
                        "type": "FDRE",
                        "parameters": {},
                        "port_directions": {
                            "C": "input", "CE": "input", "D": "input",
                            "Q": "output", "R": "input",
                        },
                        "connections": {
                            "C": [2], "CE": ["1"], "D": [4],
                            "Q": [5], "R": ["0"],
                        },
                    },
                },
                "netnames": {
                    "clk": {"bits": [2]}, "a": {"bits": [3]},
                    "n": {"bits": [4]}, "q": {"bits": [5]},
                },
            }
        }
    }
    coefficients = {
        "ff_clock_to_q": 25.0, "carry_co": 10.0,
        "lut_a1": 70.0, "lut_a2": 65.0, "lut_a3": 60.0,
        "lut_a4": 55.0, "lut_a5": 50.0, "lut_a6": 45.0,
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        mapped_path = root / "mapped.json"
        mapped_path.write_text(json.dumps(mapped), encoding="utf-8")
        timing_path = root / "routed-timing.json"
        timing = {
            "schema": "emuflow.xilinx-routed-timing/v1",
            "status": "pass", "top": "top", "part": "xcvu19p-test",
            "source": {
                "mapped_sha256": _sha(mapped_path),
                "packed_sha256": "0" * 64,
                "placement_sha256": "1" * 64,
                "route_sha256": "2" * 64,
            },
            "qualification": {
                "provider": "rapidwright-lightweight",
                "analysis": "setup-route-only",
                "hold_analysis": "unavailable",
                "hard_block_clock_timing": "unqualified",
                "logic_coefficients_ps": coefficients,
            },
            "endpoints": [{
                "id": "n4:ff/D", "net": "n4", "mapped_bit": 4,
                "driver": {"instance": "lut", "pin": "O", "site": "S0"},
                "sink": {"instance": "ff", "pin": "D", "site": "S1"},
                "route_delay_ns": 0.123,
                "binding": "exact-site-pin",
            }],
            "summary": {
                "logical_endpoints": 1, "physical_route_sinks": 1,
                "exact_site_bindings": 1, "shared_site_bindings": 0,
                "intra_site_endpoints": 0,
                "maximum_route_delay_ns": 0.123,
            },
        }
        timing_path.write_text(json.dumps(timing), encoding="utf-8")
        routed_ir, model, metadata = build_xilinx_routed_opensta_inputs(
            mapped_path, timing_path
        )
        delay_instances = [
            instance for instance in routed_ir.value["instances"]
            if instance["type"].startswith("EMUFLOW_RW_ROUTE_DELAY_")
        ]
        assert len(delay_instances) == 1
        assert metadata["inserted_route_delay_cells"] == 1
        assert model["cells"][delay_instances[0]["type"]]["delay_ns"] == 0.123
        assert model["cells"]["LUT1"]["delay_ns"] == 0.07


def test_opensta_summary_recomputes_wns_and_tns():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        output = root / "paths.json"
        output.write_text(json.dumps({"paths": [
            {"slack_ns": -2.0}, {"slack_ns": -0.5}, {"slack_ns": 1.0},
        ]}), encoding="utf-8")
        summary = root / "summary.json"
        value = {
            "schema": "emuflow.xilinx-routed-opensta-summary/v1",
            "status": "pass", "authority": "opensta",
            "source": {
                "mapped_sha256": "0" * 64,
                "routed_timing_sha256": "1" * 64,
                "timing_path_database_sha256": _sha(output),
            },
            "qor": {
                "wns_ns": -2.0, "tns_ns": -2.5,
                "failing_endpoints": 2, "timed_endpoints": 3,
            },
        }
        summary.write_text(json.dumps(value), encoding="utf-8")
        checked = validate_xilinx_routed_opensta_summary(
            summary, output_path=output
        )
        assert checked["wns_ns"] == -2.0
        assert checked["tns_ns"] == -2.5
