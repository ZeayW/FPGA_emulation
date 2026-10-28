import hashlib
import json
import os
import sqlite3
from unittest.mock import patch
import tempfile
from pathlib import Path

import pytest

import tests.routed_opensta_reference as xilinx_opensta
import emuflow.timing.xilinx_timing as xilinx_timing
from emuflow.common.errors import ValidationError
from emuflow.timing.opensta import render_opensta_liberty
from emuflow.timing.xilinx_opensta import (
    run_xilinx_routed_opensta,
)
from tests.xilinx_opensta_reference import build_xilinx_routed_opensta_inputs
from tests.routed_opensta_reference import build_xilinx_routed_opensta_indexed_inputs, validate_xilinx_routed_opensta_summary
from emuflow.ir.ir import EmuIR
from emuflow.physical.xilinx_netlist import emit_xilinx_mapped_index
from emuflow.frontend.yosys import import_yosys_json


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_routed_opensta_rejects_engine_before_expanding_inputs(tmp_path):
    mapped,timing=_indexed_routed_fixture(tmp_path)
    with pytest.raises(ValidationError, match="OpenSTA failed|semantic version"):
        run_xilinx_routed_opensta(
            mapped,
            timing,
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
        from tests import xilinx_opensta_reference as reference
        original_opensta_read = reference.read_json
        original_opensta_sha256 = reference._sha256
        opensta_reads = []
        opensta_hashes = []

        def tracked_read(path):
            opensta_reads.append(Path(path))
            return original_opensta_read(path)

        def tracked_sha256(path):
            opensta_hashes.append(Path(path))
            return original_opensta_sha256(path)

        monkeypatch.setattr(reference, "read_json", tracked_read)
        monkeypatch.setattr(reference, "_sha256", tracked_sha256)
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


def test_indexed_routed_opensta_staging_matches_graph_semantics(tmp_path):
    mapped = {
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {
                "clk": {"direction": "input", "bits": [2]},
                "a": {"direction": "input", "bits": [3]},
                "q": {"direction": "output", "bits": [5]},
            },
            "cells": {
                "lut": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [3], "O": [4]},
                },
                "ff": {
                    "type": "FDRE", "parameters": {"IS_C_INVERTED": "1"},
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
        }},
    }
    mapped_path = tmp_path / "mapped.json"
    mapped_path.write_text(json.dumps(mapped), encoding="utf-8")
    timing_path = tmp_path / "routed-timing.json"
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
            "logic_coefficients_ps": {
                "ff_clock_to_q": 25.0, "carry_co": 10.0,
                "lut_a1": 70.0, "lut_a2": 65.0, "lut_a3": 60.0,
                "lut_a4": 55.0, "lut_a5": 50.0, "lut_a6": 45.0,
            },
        },
        "endpoints": [{
            "id": "n4:ff/D", "net": "n4", "mapped_bit": 4,
            "driver": {"instance": "lut", "pin": "O", "site": "S0"},
            "sink": {"instance": "ff", "pin": "D", "site": "S1"},
            "route_delay_ns": 0.123, "binding": "exact-site-pin",
        }],
        "summary": {
            "logical_endpoints": 1, "physical_route_sinks": 1,
            "exact_site_bindings": 1, "shared_site_bindings": 0,
            "intra_site_endpoints": 0, "maximum_route_delay_ns": 0.123,
        },
    }
    timing_path.write_text(json.dumps(timing), encoding="utf-8")
    expected_ir, expected_model, expected_metadata = (
        build_xilinx_routed_opensta_inputs(mapped_path, timing_path)
    )
    routed_path = tmp_path / "routed.emuir.sqlite"
    mapped_index = tmp_path / "current-mapped.sqlite"
    source_ir = tmp_path / "current-source.emuir.sqlite"
    import_yosys_json(mapped_path, clocks=["clk"]).save(source_ir)
    emit_xilinx_mapped_index(source_ir, mapped_index)
    timing["source"]["mapped_sha256"] = _sha(mapped_index)
    timing_path.write_text(json.dumps(timing))
    _stream_timing(timing_path)
    actual_model, actual_metadata = build_xilinx_routed_opensta_indexed_inputs(
        mapped_index, timing_path, routed_path, clocks={"clk": 10.0}
    )
    actual_ir = EmuIR.load(routed_path)
    actual_value = actual_ir.to_dict()
    expected_value = expected_ir.to_dict()
    for field in ("instances", "nets"):
        actual_value[field].sort(key=lambda record: record["id"])
        expected_value[field].sort(key=lambda record: record["id"])
    # ``cut_class`` is a Phase-3 partitioning annotation.  The indexed import
    # recomputes it after inserting the timing-only route cell; compare the
    # actual circuit graph and OpenSTA model instead.
    for value in (actual_value, expected_value):
        for net in value["nets"]:
            net.pop("cut_class", None)
    assert actual_value["design"]["top"] == expected_value["design"]["top"]
    expected_value["design"] = actual_value["design"]
    assert actual_value == expected_value
    assert actual_model == expected_model
    actual_metadata = {key: value for key, value in actual_metadata.items() if key not in {"implementation_id", "native_metrics"}}
    assert actual_metadata == {
        **expected_metadata, "representation": "compact-emuir-sqlite"
    }


def _stream_timing(path):
    value = json.loads(path.read_text())
    if value["schema"] == "emuflow.xilinx-routed-timing/v2":
        return
    records = value.pop("endpoints")
    payload = path.with_name(path.name + ".endpoints.jsonl")
    payload.write_bytes(b"".join(json.dumps(row).encode()+b"\n" for row in records))
    value["schema"] = "emuflow.xilinx-routed-timing/v2"
    value["payloads"] = {"endpoints": {"format": "jsonl-object/v1", "path": payload.name, "records": len(records), "sha256": _sha(payload)}}
    path.write_text(json.dumps(value))


def _indexed_routed_fixture(tmp_path, *, registered_launch=False):
    mapped = {
        "creator": "fixture",
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {
                "clk": {"direction": "input", "bits": [2]},
                "a": {"direction": "input", "bits": [3]},
                "q": {"direction": "output", "bits": [5]},
            },
            "cells": {
                "lut": {
                    "type": "LUT1", "parameters": {"INIT": "10"},
                    "attributes": {},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": [3], "O": [4]},
                },
                "ff": {
                    "type": "FDRE", "parameters": {}, "attributes": {},
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
        }},
    }
    if registered_launch:
        # The physical STA adapter deliberately adds no implicit input delay.
        # Exercise a real clocked startpoint rather than an unconstrained port.
        module = mapped["modules"]["top"]
        module["ports"].pop("a")
        module["cells"]["launch"] = {
            "type": "FDRE", "parameters": {}, "attributes": {},
            "port_directions": {"C": "input", "CE": "input", "D": "input", "Q": "output", "R": "input"},
            "connections": {"C": [2], "CE": ["1"], "D": ["0"], "Q": [3], "R": ["0"]},
        }
    source = tmp_path / "mapped-source.json"
    source.write_text(json.dumps(mapped), encoding="utf-8")
    ir_path = tmp_path / "mapped-source.emuir.sqlite"
    import_yosys_json(source, clocks=("clk",)).save(ir_path)
    mapped_index = tmp_path / "mapped.sqlite"
    emit_xilinx_mapped_index(ir_path, mapped_index)
    timing_path = tmp_path / "routed-timing.json"
    timing_path.write_text(json.dumps({
        "schema": "emuflow.xilinx-routed-timing/v1",
        "status": "pass", "top": "top", "part": "xcvu19p-test",
        "source": {
            "mapped_sha256": _sha(mapped_index),
            "packed_sha256": "0" * 64,
            "placement_sha256": "1" * 64,
            "route_sha256": "2" * 64,
        },
        "qualification": {
            "provider": "rapidwright-lightweight",
            "analysis": "setup-route-only", "hold_analysis": "unavailable",
            "hard_block_clock_timing": "unqualified",
            "logic_coefficients_ps": {
                "ff_clock_to_q": 25.0, "carry_co": 10.0,
                "lut_a1": 70.0, "lut_a2": 65.0, "lut_a3": 60.0,
                "lut_a4": 55.0, "lut_a5": 50.0, "lut_a6": 45.0,
            },
        },
        "endpoints": [{
            "id": "n4:ff/D", "net": "n", "mapped_bit": 4,
            "driver": {"instance": "lut", "pin": "O", "site": "S0"},
            "sink": {"instance": "ff", "pin": "D", "site": "S1"},
            "route_delay_ns": 0.123, "binding": "exact-site-pin",
        }],
        "summary": {
            "logical_endpoints": 1, "physical_route_sinks": 1,
            "exact_site_bindings": 1, "shared_site_bindings": 0,
            "intra_site_endpoints": 0, "maximum_route_delay_ns": 0.123,
        },
    }), encoding="utf-8")
    _stream_timing(timing_path)
    return mapped_index, timing_path


def test_routed_opensta_stages_directly_from_mapped_sqlite(tmp_path):
    mapped_index, timing_path = _indexed_routed_fixture(tmp_path)
    routed_path = tmp_path / "routed.emuir.sqlite"
    model, metadata = build_xilinx_routed_opensta_indexed_inputs(
        mapped_index, timing_path, routed_path, clocks={"clk": 10.0}
    )
    routed = EmuIR.load(routed_path)
    assert not (tmp_path / "mapped.json").exists()
    assert metadata["inserted_route_delay_cells"] == 1
    assert model["cells"]["EMUFLOW_RW_ROUTE_DELAY_000000"]["delay_ns"] == 0.123
    assert any(
        item["type"] == "EMUFLOW_RW_ROUTE_DELAY_000000"
        for item in routed.value["instances"]
    )
    original = next(net for net in routed.value["nets"] if net["id"] == "n")
    delayed = next(net for net in routed.value["nets"] if net["id"] == "__emuflow_rw_delay_net__00000000")
    assert original["sinks"] == [{"instance": "__emuflow_rw_delay__00000000", "port": "A", "bit": 0}]
    assert delayed["drivers"] == [{"instance": "__emuflow_rw_delay__00000000", "port": "Y", "bit": 0}]
    assert delayed["sinks"] == [{"instance": "ff", "port": "D", "bit": 0}]
    assert delayed["aliases"] == []
    assert "yosys_bit" not in delayed


def test_typed_routed_connectivity_matches_independent_graph_oracle(tmp_path):
    from emuflow.physical.xilinx_netlist import IndexedXilinxMappedNetlist
    mapped, timing = _indexed_routed_fixture(tmp_path)
    routed = tmp_path / "routed.emuir.sqlite"
    model, _metadata = build_xilinx_routed_opensta_indexed_inputs(mapped, timing, routed, clocks={"clk": 10.0})
    source = tmp_path / "reference-mapped.json"
    with IndexedXilinxMappedNetlist(mapped) as view:
        module = {**view.module, "cells": dict(view.cells.items()), "netnames": dict(view.netnames.items())}
        source.write_text(json.dumps({"creator": view.creator, "modules": {view.top: module}}))
    value = json.loads(timing.read_text())
    value["source"]["mapped_sha256"] = _sha(source)
    reference_timing = tmp_path / "reference-timing.json"
    reference_timing.write_text(json.dumps(value))
    reference, expected_model, _ = build_xilinx_routed_opensta_inputs(source, reference_timing)
    actual = EmuIR.load(routed)
    for field in ("instances", "nets", "ports", "clocks"):
        lhs, rhs = actual.to_dict()[field], reference.to_dict()[field]
        if field == "nets":
            for net in (*lhs, *rhs):
                net.pop("cut_class", None)  # independent importer recomputes this annotation
        assert sorted(lhs, key=lambda x: x["id"]) == sorted(rhs, key=lambda x: x["id"])
    assert model == expected_model




@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENSTA"), reason="real OpenSTA not configured")
@pytest.mark.parametrize("inverted", [False, True])
def test_real_opensta_accounts_for_each_inserted_route_delay(tmp_path, inverted):
    mapped, timing = _indexed_routed_fixture(tmp_path, registered_launch=True)
    original = json.loads(timing.read_text())
    if inverted:
        with sqlite3.connect(mapped) as connection:
            connection.execute("UPDATE cells SET record_json=json_set(record_json,'$.parameters.IS_C_INVERTED','1') WHERE id='ff'")
        original["source"]["mapped_sha256"] = _sha(mapped)
    records = [json.loads(line) for line in (timing.parent / original["payloads"]["endpoints"]["path"]).read_text().splitlines()]
    payload = tmp_path / "routed-endpoints.jsonl"
    original["schema"] = "emuflow.xilinx-routed-timing/v2"
    original["payloads"] = {"endpoints": {"format": "jsonl-object/v1", "path": payload.name, "records": len(records)}}
    results = []
    for index, delay in enumerate((0.0, 0.123)):
        records[0]["route_delay_ns"] = delay
        payload.write_bytes(b"".join(json.dumps(row).encode()+b"\n" for row in records))
        original["payloads"]["endpoints"]["sha256"] = _sha(payload)
        original["summary"]["maximum_route_delay_ns"] = delay
        timing.write_text(json.dumps(original))
        summary = tmp_path / f"summary-{index}.json"
        with patch("tests.routed_opensta_stage_reference._TimingCells.items", side_effect=AssertionError("Python delay model expansion")):
            run_xilinx_routed_opensta(mapped, timing, tmp_path / f"paths-{index}.json", summary,
                                     clocks={"clk": 0.1}, executable=os.environ["EMUFLOW_TEST_OPENSTA"])
        results.append(json.loads(summary.read_text())["qor"])
    assert results[1]["wns_ns"] == pytest.approx(results[0]["wns_ns"] - 0.123, abs=1e-3)


def test_typed_staging_transform_owns_pin_roles_bits_and_constants(tmp_path):
    from emuflow.frontend.yosys import import_yosys_json_indexed
    mapped, _timing = _indexed_routed_fixture(tmp_path)
    output = tmp_path / "transformed.emuir.sqlite"
    def transform(connection, *_):
        row = connection.execute("SELECT record_json FROM staging_instances WHERE id='ff'").fetchone()
        cell = json.loads(row[0])
        cell["connections"]["CE"] = ["0"]
        cell["connections"]["R"] = ["1"]
        cell["connections"]["D"] = [6, 7]
        cell["connections"]['quoted.pin"/x'] = [3, "1"]
        cell["port_directions"]['quoted.pin"/x'] = "inout"
        connection.execute("UPDATE staging_instances SET record_json=? WHERE id='ff'", (json.dumps(cell),))
        return {"fixture": True}
    import_yosys_json_indexed(mapped, output, top="top", clocks=["clk"], _source_index_path=mapped, staging_transform=transform)
    ir = EmuIR.load(output)
    ff = next(cell for cell in ir.value["instances"] if cell["id"] == "ff")
    assert ff["constant_connections"] == [{"port": "CE", "bit": 0, "value": "0"}, {"port": "R", "bit": 0, "value": "1"}, {"port": 'quoted.pin"/x', "bit": 1, "value": "1"}]
    with sqlite3.connect(output) as db:
        seen = db.execute("SELECT role,port,bit FROM endpoints WHERE instance_id='ff' AND port=? ORDER BY role", ('quoted.pin"/x',)).fetchall()
    assert seen == [(0, 'quoted.pin"/x', 0), (1, 'quoted.pin"/x', 0)]
    assert any(endpoint["port"] == "D" and endpoint["bit"] == 1 for net in ir.value["nets"] for endpoint in net["sinks"])


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
