import hashlib
import json
import tempfile
from pathlib import Path
import pytest

from emuflow.timing.boundary_timing_storage import read_boundary_timing_storage
from emuflow.common.errors import ValidationError

import tests.xilinx_segment_timing_reference as xilinx_segment_timing
from tests.xilinx_segment_timing_reference import (
    build_xilinx_boundary_timing,
    build_xilinx_local_path_timing,
    build_xilinx_segment_timing_bundle,
)
from emuflow.timing.local_path_timing import (
    LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
    local_path_timing_header,
)
from emuflow.timing.local_path_timing_storage import (read_local_path_timing_storage)
from tests.timing_facts import local_timing_facts
from tests.routed_timing_adapter import (
    XILINX_ROUTED_TIMING_PAYLOAD_FORMAT,
    XILINX_ROUTED_TIMING_STREAM_SCHEMA,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_declared_undriven_inputs_remain_disconnected_and_queried_paths_fail():
    mapped = {"modules": {"top": {
        "ports": {"i": {"direction": "input", "bits": [2]},
                  "o": {"direction": "output", "bits": [4]}},
        "netnames": {"floating": {"bits": [3]}},
        "cells": {"lut": {"type": "LUT2",
            "port_directions": {"I0": "input", "I1": "input", "O": "output"},
            "connections": {"I0": [2], "I1": [3], "O": [4]}}},
    }}}
    timing = {"top": "top", "qualification": {"logic_coefficients_ps": {
        "lut_a1": 70.0, "ff_clock_to_q": 25.0, "carry_co": 10.0,
    }}}
    with pytest.raises(ValidationError, match="lacks one driver"):
        xilinx_segment_timing._TimingGraph(mapped, timing, [])
    graph = xilinx_segment_timing._TimingGraph(mapped, timing, [], {"floating"})
    assert graph.undriven_bits == {3: "floating"}
    assert len(graph.architectural_sources()) == 1
    assert graph.longest(graph.architectural_sources(), "top:o")[0] == pytest.approx(0.07)
    with pytest.raises(ValidationError, match="no path"):
        graph.longest(graph.architectural_sources(), "cell:lut/I1")
    # A declaration cannot mask multiple drivers introduced by a broken map.
    mapped["modules"]["top"]["ports"].update({
        "bad_a": {"direction": "input", "bits": [3]},
        "bad_b": {"direction": "input", "bits": [3]},
    })
    with pytest.raises(ValidationError, match="lacks one driver"):
        xilinx_segment_timing._TimingGraph(mapped, timing, [], {"floating"})


def test_undriven_declaration_reader_streams_completed_sqlite_source(tmp_path):
    from tests.yosys_reference import import_yosys_json
    from emuflow.ir.ir import EmuIR
    value = import_yosys_json(
        Path(__file__).resolve().parents[1] / "examples/yosys/counter.json",
        top="counter", clocks=["clk"],
    ).to_dict()
    net = value["nets"][0]
    net["drivers"] = []
    net["cut_class"] = "undriven"
    path = tmp_path / "source.emuir.sqlite"
    EmuIR(value).save(path)
    assert net["id"] in xilinx_segment_timing._declared_undriven_nets(path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nets": [{"id": "n", "cut_class": "undriven", "drivers": [{}]}]}))
    with pytest.raises(ValidationError, match="has a driver"):
        xilinx_segment_timing._declared_undriven_nets(bad)


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
                {"id": "n2:rx_lut/I0", "sink": {"instance": "rx_lut", "pin": "I0"},
                 "route_delay_ns": 0.11},
                {"id": "n3:shadow/D", "sink": {"instance": "shadow", "pin": "D"},
                 "route_delay_ns": 0.2},
                {"id": "n4:tx_lut/I0", "sink": {"instance": "tx_lut", "pin": "I0"},
                 "route_delay_ns": 0.3},
            ],
            "virtual_io": [
                {"id": "rx-io", "direction": "rx", "logical_port": "rx",
                 "logical_bit": 0, "p_site": "IOB_X0Y0", "n_site": "IOB_X0Y1",
                 "route_delay_ns": 0.11,
                 "measurement": "fixed-iob-routed-boundary"},
                {"id": "tx-io", "direction": "tx", "logical_port": "tx",
                 "logical_bit": 0, "p_site": "IOB_X0Y2", "n_site": "IOB_X0Y3",
                 "route_delay_ns": 0.4,
                 "measurement": "fixed-iob-routed-boundary"},
            ],
            "summary": {
                "logical_endpoints": 3, "physical_route_sinks": 3,
                "exact_site_bindings": 3, "shared_site_bindings": 0,
                "intra_site_endpoints": 0, "maximum_route_delay_ns": 0.3,
            },
        }
        timing_path = root / "timing.json"
        timing_path.write_text(json.dumps(timing), encoding="utf-8")
        identity_path = root / "identity.json"
        from emuflow.timing.boundary_identity_storage import write_boundary_identity_storage
        from tests.test_boundary_identity_storage import _record
        records=[]
        for i, fact in enumerate(identity['endpoints']):
            base=_record(fact['id'],fact['kind'],i)
            base.update(fact)
            base['merged_ir']={**_record(fact['id'],fact['kind'],i)['merged_ir'],**fact['merged_ir']}
            records.append(base)
        with write_boundary_identity_storage(identity_path,
                {k:v for k,v in identity.items() if k!='endpoints'}, records):pass
        output = root / "boundary.json"
        result = build_xilinx_boundary_timing(
            identity_path, mapped_path, timing_path, output
        )
        database = json.loads(output.read_text())
        with read_boundary_timing_storage(output) as boundary_view:
            delays = {
                item["id"]: item["delay_ns"]
                for item in boundary_view["endpoints"]
            }
        endpoints = timing.pop("endpoints")
        encoded = b"".join(
            json.dumps(
                endpoint, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            for endpoint in endpoints
        )
        payload_path = root / "timing.json.endpoints.jsonl"
        payload_path.write_bytes(encoded)
        timing["schema"] = XILINX_ROUTED_TIMING_STREAM_SCHEMA
        timing["payloads"] = {
            "endpoints": {
                "format": XILINX_ROUTED_TIMING_PAYLOAD_FORMAT,
                "path": payload_path.name,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "records": len(endpoints),
            }
        }
        timing_path.write_text(json.dumps(timing), encoding="utf-8")
        streamed_output = root / "boundary-streamed.json"
        streamed_result = build_xilinx_boundary_timing(
            identity_path, mapped_path, timing_path, streamed_output
        )
        streamed_database = json.loads(streamed_output.read_text())
    assert result["endpoints"] == 2
    assert streamed_result["endpoints"] == 2
    assert streamed_database["logical_sha256"] == database["logical_sha256"]
    assert abs(delays["rx0"] - 0.38) < 1e-12
    assert abs(delays["tx0"] - 0.795) < 1e-12


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
        with local_timing_facts(
            identity_path,
            local_path_timing_header(
                identity,
                provider=identity["provider"],
                qualification=LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
            ),
            [{**identity["paths"][0], "delay_ns": 0.0}],
        ):
            pass
        output = root / "local.json"
        result = build_xilinx_local_path_timing(
            identity_path, mapped_path, timing_path, output
        )
        with read_local_path_timing_storage(output) as database:
            assert result["local_paths"] == 1
            assert result["measurement_counts"] == {
                "explicit-routed-path-chain": 0,
                "endpoint-longest-path-fallback": 1,
            }
            assert abs(database["paths"][0]["delay_ns"] - 0.595) < 1e-12


def test_segment_timing_bundle_loads_the_routed_graph_once(
    monkeypatch, tmp_path,
):
    graph = object()
    loads = []
    projections = []

    def load_graph(mapped_path, timing_path):
        loads.append((mapped_path, timing_path))
        return graph

    def project(name):
        def implementation(*_args, _timing_graph=None, **_kwargs):
            assert _timing_graph is graph
            projections.append(name)
            return {"status": "pass", "kind": name}
        return implementation

    monkeypatch.setattr(xilinx_segment_timing, "_graph", load_graph)
    monkeypatch.setattr(
        xilinx_segment_timing,
        "build_xilinx_boundary_timing",
        project("boundary"),
    )
    monkeypatch.setattr(
        xilinx_segment_timing,
        "build_xilinx_logic_segment_timing",
        project("logic_segment"),
    )
    monkeypatch.setattr(
        xilinx_segment_timing,
        "build_xilinx_local_path_timing",
        project("local_path"),
    )
    result = build_xilinx_segment_timing_bundle(
        boundary_identity_path=tmp_path / "boundary-identity.json",
        mapped_path=tmp_path / "mapped.json",
        timing_path=tmp_path / "timing.json",
        boundary_output_path=tmp_path / "boundary.json",
        logic_identity_path=tmp_path / "logic-identity.json",
        logic_output_path=tmp_path / "logic.json",
        local_identity_path=tmp_path / "local-identity.json",
        local_output_path=tmp_path / "local.json",
    )
    assert len(loads) == 1
    assert projections == ["boundary", "logic_segment", "local_path"]
    assert set(result) == {"boundary", "logic_segment", "local_path"}
