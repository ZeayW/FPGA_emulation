import hashlib
import json
import tempfile
from pathlib import Path

from emuflow.xilinx_segment_timing import build_xilinx_boundary_timing


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_boundary_timing_covers_rx_and_tx_with_routed_delays():
    mapped = {
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {
                "rx": {"direction": "input", "bits": [2]},
                "tx": {"direction": "output", "bits": [5]},
                "clk": {"direction": "input", "bits": [6]},
            },
            "cells": {
                "rx_lut": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [2], "O": [3]},
                },
                "shadow": {
                    "type": "FDRE", "parameters": {},
                    "port_directions": {
                        "C": "input", "CE": "input", "D": "input",
                        "Q": "output", "R": "input",
                    },
                    "connections": {
                        "C": [6], "CE": ["1"], "D": [3],
                        "Q": [4], "R": ["0"],
                    },
                },
                "tx_lut": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [4], "O": [5]},
                },
            },
            "netnames": {name: {"bits": [bit]} for name, bit in (
                ("rx", 2), ("rx_data", 3), ("state", 4),
                ("tx", 5), ("clk", 6),
            )},
        }}
    }
    coefficients = {
        "ff_clock_to_q": 25.0, "carry_co": 10.0,
        "lut_a1": 70.0, "lut_a2": 65.0, "lut_a3": 60.0,
        "lut_a4": 55.0, "lut_a5": 50.0, "lut_a6": 45.0,
    }
    identity = {
        "schema": "emuflow.boundary-identity/v1", "status": "pass",
        "design": "d", "platform": "p", "fpga": "fpga0",
        "provider": "test", "coverage": {
            "endpoints": 2, "tx": 1, "rx": 1, "external_port_nets": 2,
        },
        "endpoints": [
            {
                "id": "rx0", "kind": "rx", "schedule_entry": "s0",
                "merged_ir": {"external_port": "rx", "external_port_bit": 0,
                              "boundary_register_instances": ["shadow"]},
            },
            {
                "id": "tx0", "kind": "tx", "schedule_entry": "s1",
                "merged_ir": {"external_port": "tx", "external_port_bit": 0,
                              "boundary_register_instances": []},
            },
        ],
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        mapped_path = root / "mapped.json"
        mapped_path.write_text(json.dumps(mapped), encoding="utf-8")
        timing = {
            "schema": "emuflow.xilinx-routed-timing/v1", "status": "pass",
            "top": "top", "part": "xcvu19p-test",
            "source": {
                "mapped_sha256": _sha(mapped_path),
                "packed_sha256": "0" * 64, "placement_sha256": "1" * 64,
                "route_sha256": "2" * 64,
            },
            "qualification": {
                "provider": "rapidwright-lightweight",
                "analysis": "setup-route-only", "hold_analysis": "unavailable",
                "hard_block_clock_timing": "unqualified",
                "logic_coefficients_ps": coefficients,
            },
            "endpoints": [
                {"id": "n3:shadow/D", "sink": {"instance": "shadow", "pin": "D"},
                 "route_delay_ns": 0.2},
                {"id": "n4:tx_lut/I0", "sink": {"instance": "tx_lut", "pin": "I0"},
                 "route_delay_ns": 0.3},
            ],
            "summary": {
                "logical_endpoints": 2, "physical_route_sinks": 2,
                "exact_site_bindings": 2, "shared_site_bindings": 0,
                "intra_site_endpoints": 0, "maximum_route_delay_ns": 0.3,
            },
        }
        timing_path = root / "timing.json"
        timing_path.write_text(json.dumps(timing), encoding="utf-8")
        identity_path = root / "identity.json"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        output = root / "boundary.json"
        result = build_xilinx_boundary_timing(
            identity_path, mapped_path, timing_path, output
        )
        database = json.loads(output.read_text())
    assert result["endpoints"] == 2
    delays = {item["id"]: item["delay_ns"] for item in database["endpoints"]}
    assert abs(delays["rx0"] - 0.27) < 1e-12
    assert abs(delays["tx0"] - 0.395) < 1e-12
