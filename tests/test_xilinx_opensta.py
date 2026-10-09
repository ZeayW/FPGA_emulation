import hashlib
import json
import tempfile
from pathlib import Path

import pytest

import emuflow.xilinx_opensta as xilinx_opensta
import emuflow.xilinx_timing as xilinx_timing
from emuflow.errors import ValidationError
from emuflow.opensta import render_opensta_liberty
from emuflow.xilinx_opensta import (
    build_xilinx_routed_opensta_inputs,
    run_xilinx_routed_opensta,
    validate_xilinx_routed_opensta_summary,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_routed_opensta_rejects_engine_before_expanding_inputs(tmp_path):
    with pytest.raises(ValidationError, match="semantic version"):
        run_xilinx_routed_opensta(
            tmp_path / "missing-mapped.json",
            tmp_path / "missing-timing.json",
            tmp_path / "paths.json",
            tmp_path / "summary.json",
            clocks={"clk": 10.0},
            executable="/usr/bin/false",
        )


def test_routed_opensta_staging_inserts_one_exact_delay_per_sink(monkeypatch):
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
                        "parameters": {"IS_C_INVERTED": "1"},
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
        original_opensta_read = xilinx_opensta.read_json
        original_opensta_sha256 = xilinx_opensta._sha256
        opensta_reads = []
        opensta_hashes = []

        def tracked_read(path):
            opensta_reads.append(Path(path))
            return original_opensta_read(path)

        def tracked_sha256(path):
            opensta_hashes.append(Path(path))
            return original_opensta_sha256(path)

        monkeypatch.setattr(xilinx_opensta, "read_json", tracked_read)
        monkeypatch.setattr(xilinx_opensta, "_sha256", tracked_sha256)
        monkeypatch.setattr(
            xilinx_timing,
            "read_json",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("timing validator reparsed a preloaded input")
            ),
        )
        monkeypatch.setattr(
            xilinx_timing,
            "_sha256",
            lambda _path: (_ for _ in ()).throw(
                AssertionError("timing validator rehashed a sealed input")
            ),
        )
        routed_ir, model, metadata = build_xilinx_routed_opensta_inputs(
            mapped_path, timing_path
        )
        assert opensta_reads == [mapped_path, timing_path]
        assert opensta_hashes == [timing_path, mapped_path]
        delay_instances = [
            instance for instance in routed_ir.value["instances"]
            if instance["type"].startswith("EMUFLOW_RW_ROUTE_DELAY_")
        ]
        assert len(delay_instances) == 1
        assert metadata["logical_route_endpoints"] == 1
        assert metadata["inserted_route_delay_cells"] == 1
        assert model["cells"][delay_instances[0]["type"]]["delay_ns"] == 0.123
        assert model["cells"]["LUT1"]["delay_ns"] == 0.07
        ff = next(
            instance for instance in routed_ir.value["instances"]
            if instance["id"] == "ff"
        )
        assert ff["type"] == "FDRE__NEG"
        assert metadata["falling_edge_ff_instances"] == 1
        liberty = render_opensta_liberty(model)
        assert "cell (FDRE__NEG)" in liberty
        assert 'clocked_on : "!C";' in liberty
        assert "timing_type : setup_falling;" in liberty
        assert "timing_type : falling_edge;" in liberty
        assert delay_instances[0]["id"] == "__emuflow_rw_delay__00000000"
        assert "/" not in delay_instances[0]["id"]
        delay_nets = [
            net for net in routed_ir.value["nets"]
            if net["id"].startswith("__emuflow_rw_delay_net__")
        ]
        assert [net["id"] for net in delay_nets] == [
            "__emuflow_rw_delay_net__00000000"
        ]


def test_dsp_timing_cell_unions_ports_across_cascade_instances():
    mapped = {
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {},
            "cells": {
                "dsp_head": {
                    "type": "DSP48E2", "parameters": {},
                    "port_directions": {
                        "A": "input", "ACOUT": "output", "P": "output",
                    },
                    "connections": {
                        "A": [1, "0"], "ACOUT": [10, 11], "P": [20, 21],
                    },
                },
                "dsp_tail": {
                    "type": "DSP48E2", "parameters": {},
                    "port_directions": {
                        "A": "input", "ACIN": "input", "P": "output",
                    },
                    "connections": {
                        "A": [2, "0"], "ACIN": [10, 11], "P": [30, 31],
                    },
                },
            },
            "netnames": {},
        }},
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        mapped_path = root / "mapped.json"
        mapped_path.write_text(json.dumps(mapped), encoding="utf-8")
        timing_path = root / "routed-timing.json"
        timing_path.write_text(json.dumps({
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
                "logic_coefficients_ps": {
                    "ff_clock_to_q": 25.0, "carry_co": 10.0,
                    "lut_a1": 70.0, "lut_a2": 65.0, "lut_a3": 60.0,
                    "lut_a4": 55.0, "lut_a5": 50.0, "lut_a6": 45.0,
                },
            },
            "endpoints": [],
            "summary": {
                "logical_endpoints": 0, "physical_route_sinks": 0,
                "exact_site_bindings": 0, "shared_site_bindings": 0,
                "intra_site_endpoints": 0, "maximum_route_delay_ns": 0.0,
            },
        }), encoding="utf-8")
        _ir, model, _metadata = build_xilinx_routed_opensta_inputs(
            mapped_path, timing_path
        )
    dsp = model["cells"]["DSP48E2"]
    assert {"A__0", "A__1", "ACIN__0", "ACIN__1"} <= set(dsp["inputs"])
    assert {"ACOUT__0", "ACOUT__1", "P__0", "P__1"} <= set(dsp["outputs"])


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


def test_opensta_summary_streams_sealed_path_payload():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        output = root / "paths.json"
        payload = root / "paths.json.paths.jsonl"
        records = [
            {"slack_ns": -2.0},
            {"slack_ns": -0.5},
            {"slack_ns": 1.0},
        ]
        encoded = b"".join(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
            for item in records
        )
        payload.write_bytes(encoded)
        output.write_text(json.dumps({
            "schema": "emuflow.sta-path-database/v2",
            "payloads": {"paths": {
                "format": "jsonl-object/v1",
                "path": payload.name,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "records": 3,
            }},
        }), encoding="utf-8")
        summary = root / "summary.json"
        summary.write_text(json.dumps({
            "schema": "emuflow.xilinx-routed-opensta-summary/v1",
            "status": "pass",
            "authority": "opensta",
            "source": {
                "mapped_sha256": "0" * 64,
                "routed_timing_sha256": "1" * 64,
                "timing_path_database_sha256": _sha(output),
            },
            "qor": {
                "wns_ns": -2.0,
                "tns_ns": -2.5,
                "failing_endpoints": 2,
                "timed_endpoints": 3,
            },
        }), encoding="utf-8")
        checked = validate_xilinx_routed_opensta_summary(
            summary, output_path=output
        )
        assert checked["wns_ns"] == -2.0
        assert checked["tns_ns"] == -2.5


def test_opensta_summary_reuses_preloaded_path_database(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        output = root / "paths.json"
        database = {"paths": [
            {"slack_ns": -1.25}, {"slack_ns": 0.75},
        ]}
        output.write_text(json.dumps(database), encoding="utf-8")
        summary = root / "summary.json"
        summary.write_text(json.dumps({
            "schema": "emuflow.xilinx-routed-opensta-summary/v1",
            "status": "pass",
            "authority": "opensta",
            "source": {
                "mapped_sha256": "0" * 64,
                "routed_timing_sha256": "1" * 64,
                "timing_path_database_sha256": _sha(output),
            },
            "qor": {
                "wns_ns": -1.25,
                "tns_ns": -1.25,
                "failing_endpoints": 1,
                "timed_endpoints": 2,
            },
        }), encoding="utf-8")
        original_read_json = xilinx_opensta.read_json

        def guarded_read_json(path):
            if Path(path) == output:
                raise AssertionError("preloaded path database was reparsed")
            return original_read_json(path)

        monkeypatch.setattr(xilinx_opensta, "read_json", guarded_read_json)
        checked = validate_xilinx_routed_opensta_summary(
            summary, output_path=output, _database=database,
        )
        assert checked["wns_ns"] == -1.25
