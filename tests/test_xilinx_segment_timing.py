import hashlib
import json
import tempfile
from pathlib import Path

from emuflow.xilinx_segment_timing import (
    build_xilinx_boundary_timing,
    build_xilinx_local_path_timing,
)


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


def test_local_path_timing_is_source_bound_and_uses_routed_graph():
    mapped = {
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {"clk": {"direction": "input", "bits": [2]}},
            "cells": {
                "launch": {
                    "type": "FDRE", "parameters": {},
                    "port_directions": {
                        "C": "input", "CE": "input", "D": "input",
                        "Q": "output", "R": "input",
                    },
                    "connections": {
                        "C": [2], "CE": ["1"], "D": ["0"],
                        "Q": [3], "R": ["0"],
                    },
                },
                "logic": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [3], "O": [4]},
                },
                "capture": {
                    "type": "FDRE", "parameters": {},
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
            "netnames": {},
        }}
    }
    coefficients = {
        "ff_clock_to_q": 25.0, "carry_co": 10.0,
        "lut_a1": 70.0, "lut_a2": 65.0, "lut_a3": 60.0,
        "lut_a4": 55.0, "lut_a5": 50.0, "lut_a6": 45.0,
    }
    source = {
        "path_database_sha256": "0" * 64,
        "original_ir_sha256": "1" * 64,
        "assignment_sha256": "2" * 64,
        "routes_sha256": "3" * 64,
        "original_paths": 1,
        "original_path_ids_sha256": "4" * 64,
    }
    identity = {
        "schema": "emuflow.local-path-identity/v2",
        "status": "pass", "design": "d", "fpga": "fpga0",
        "provider": "test", "source": source,
        "coverage": {"local_paths": 1},
        "paths": [{
            "id": "p0", "kind": "local", "fpga": "fpga0",
            "clock_domain": "clk", "clock_period_ns": 10.0,
            "required_time_ns": 9.0,
            "start_pin": "cell:launch/Q",
            "end_pin": "cell:capture/D",
            "measurement": "endpoint-longest-path-fallback",
            "path_pins": [],
        }],
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
                "packed_sha256": "5" * 64,
                "placement_sha256": "6" * 64,
                "route_sha256": "7" * 64,
            },
            "qualification": {
                "provider": "rapidwright-lightweight",
                "analysis": "setup-route-only",
                "hold_analysis": "unavailable",
                "hard_block_clock_timing": "unqualified",
                "logic_coefficients_ps": coefficients,
            },
            "endpoints": [
                {"id": "n3:logic/I0",
                 "sink": {"instance": "logic", "pin": "I0"},
                 "route_delay_ns": 0.2},
                {"id": "n4:capture/D",
                 "sink": {"instance": "capture", "pin": "D"},
                 "route_delay_ns": 0.3},
            ],
            "summary": {
                "logical_endpoints": 2, "physical_route_sinks": 2,
                "exact_site_bindings": 2, "shared_site_bindings": 0,
                "intra_site_endpoints": 0,
                "maximum_route_delay_ns": 0.3,
            },
        }
        timing_path = root / "timing.json"
        timing_path.write_text(json.dumps(timing), encoding="utf-8")
        identity_path = root / "identity.json"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        output = root / "local.json"
        result = build_xilinx_local_path_timing(
            identity_path, mapped_path, timing_path, output
        )
        database = json.loads(output.read_text())
    assert result["local_paths"] == 1
    assert result["measurement_counts"] == {
        "explicit-routed-path-chain": 0,
        "endpoint-longest-path-fallback": 1,
    }
    assert abs(database["paths"][0]["delay_ns"] - 0.595) < 1e-12
