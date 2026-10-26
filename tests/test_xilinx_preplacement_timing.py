import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from emuflow.cut_segment_qualification import (
    build_cut_segment_qualification_value,
)
from emuflow.opensta import (
    _instance_pin_sets,
    build_architecture_opensta_timing_model,
    classify_through_net_timing_endpoints,
    validate_timing_model_coverage,
)
from emuflow.xilinx_preplacement_timing import (
    build_xilinx_preplacement_timing_db,
)
from emuflow.yosys import import_yosys_json
from tests.xilinx_timing_fixture import mock_ff_control_timing


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapped_fixture(path: Path) -> None:
    cells = {
        "carry": {
            "type": "CARRY8", "parameters": {},
            "port_directions": {"CI": "input", "O": "output"},
            "connections": {"CI": [3], "O": [4]},
        },
        "lut": {
            "type": "LUT6_2", "parameters": {},
            "port_directions": {"I0": "input", "O5": "output", "O6": "output"},
            "connections": {"I0": [4], "O5": [5], "O6": [6]},
        },
        "mux": {
            "type": "MUXF9", "parameters": {},
            "port_directions": {"I0": "input", "I1": "input", "S": "input", "O": "output"},
            "connections": {"I0": [5], "I1": [6], "S": [3], "O": [7]},
        },
        "ram": {
            "type": "RAMB18E2", "parameters": {},
            "port_directions": {"CLKARDCLK": "input", "ADDR": "input", "DOUT": "output"},
            "connections": {"CLKARDCLK": [2], "ADDR": [7], "DOUT": [8]},
        },
        "dsp": {
            "type": "DSP48E2", "parameters": {},
            "port_directions": {"A": "input", "P": "output"},
            "connections": {"A": [8], "P": [9]},
        },
        "ff": {
            "type": "FDRE", "parameters": {"IS_C_INVERTED": "1"},
            "port_directions": {"C": "input", "CE": "input", "D": "input", "Q": "output", "R": "input"},
            "connections": {"C": [2], "CE": ["1"], "D": [9], "Q": [10], "R": ["0"]},
        },
    }
    value = {"modules": {"top": {
        "attributes": {"top": "1"},
        "ports": {
            "clk": {"direction": "input", "bits": [2]},
            "a": {"direction": "input", "bits": [3]},
            "q": {"direction": "output", "bits": [10]},
        },
        "cells": cells,
        "netnames": {
            "clk": {"bits": [2]}, "a": {"bits": [3]},
            **{f"n{bit}": {"bits": [bit]} for bit in range(4, 11)},
        },
    }}}
    path.write_text(json.dumps(value), encoding="utf-8")


def test_build_and_consume_rapidwright_preplacement_timing_db():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        timing = root / "timing"
        timing.mkdir()
        intra = timing / "intrasite_delay_terms.txt"
        intra.write_text("""
bel A6LUT,B6LUT,C6LUT,D6LUT,E6LUT,F6LUT,G6LUT,H6LUT in SLICEM,SLICEL
A1 O6 150
bel A5LUT,B5LUT,C5LUT,D5LUT,E5LUT,F5LUT,G5LUT,H5LUT in SLICEM,SLICEL
A1 O5 165
bel F7MUX_AB,F7MUX_CD,F7MUX_EF,F7MUX_GH in SLICEM,SLICEL
S0 OUT 77
bel AFF,BFF,CFF,DFF,EFF,FFF,GFF,HFF,AFF2,BFF2,CFF2,DFF2,EFF2,FFF2,GFF2,HFF2 in SLICEM,SLICEL
CLK Q 77
CLK D -25
bel CARRY8 in SLICEM,SLICEL
CI O7 231
bel RAMB36E2
CLKARDCLK DOUTADOUT[0] 1003
ADDRARDADDR[0] CLKARDCLK 445
bel URAM288
CLK DOUT_A[0] 1200
CLK ADDR_A[0] 500
""", encoding="utf-8")
        inter = timing / "intersite_delay_terms.txt"
        inter.write_text(
            "SITEPIN_A1_DELAY 74\nIO_LONG 300\n", encoding="utf-8"
        )
        hashes = {
            "intersite_delay_terms.txt": _sha(inter),
            "intrasite_delay_terms.txt": _sha(intra),
        }
        database = root / "xilinx-preplacement.json"
        controls = root / "controls.json"
        controls.write_text(json.dumps(mock_ff_control_timing()))
        with patch.dict(
            "emuflow.xilinx_preplacement_timing.RAPIDWRIGHT_TIMING_DATA_SHA256",
            hashes,
            clear=True,
        ):
            report = build_xilinx_preplacement_timing_db(timing, database, ff_control_timing_path=controls)
            assert report["status"] == "pass"
            mapped = root / "mapped.json"
            _mapped_fixture(mapped)
            # Known library types need no design-derived pin sets. Every
            # occurrence of a specialized type must contribute its pins.
            value = json.loads(mapped.read_text(encoding="utf-8"))
            cells = value["modules"]["top"]["cells"]
            for index in range(2000):
                cells[f"repeat_lut_{index}"] = {
                    "type": "LUT6_2", "parameters": {},
                    "port_directions": {
                        "I0": "input", "O5": "output", "O6": "output",
                    },
                    "connections": {
                        "I0": ["1"],
                        "O5": [11 + index * 2],
                        "O6": [12 + index * 2],
                    },
                }
                cells[f"known_lut_{index}"] = {
                    "type": "LUT1", "parameters": {},
                    "port_directions": {"I0": "input", "O": "output"},
                    "connections": {"I0": ["0"], "O": [4011 + index]},
                }
            mapped.write_text(json.dumps(value), encoding="utf-8")
            ir = import_yosys_json(mapped, top="top", clocks=["clk"])
            with patch(
                "emuflow.opensta._instance_pin_sets", wraps=_instance_pin_sets,
            ) as pin_sets:
                model, types = build_architecture_opensta_timing_model(ir, database)
            selected = pin_sets.call_args.kwargs['instance_ids']
            assert {'carry', 'lut', 'mux', 'ram', 'dsp', 'repeat_lut_1999'} <= selected
            assert not any(name.startswith('known_lut_') for name in selected)
            # Compare all cells/arcs and per-instance types against the old
            # exhaustive pin collection, not merely aggregate timing scalars.
            with patch(
                "emuflow.opensta._instance_pin_sets",
                side_effect=lambda graph, **kwargs: _instance_pin_sets(graph),
            ):
                reference = build_architecture_opensta_timing_model(ir, database)
            assert (model, types) == reference
            from emuflow.indexed_timing import IndexedTimingView
            from emuflow.verilog import mapped_verilog, write_mapped_verilog
            compact = root / "design.emuir.sqlite"
            ir.save(compact)
            view = IndexedTimingView(compact)
            try:
                compact_model, compact_types = build_architecture_opensta_timing_model(view, database)
                assert compact_model == model
                assert dict(compact_types) == types
                streamed = root / "streamed.v"
                write_mapped_verilog(view, streamed, timing_only=True, timing_cell_types=compact_types)
                assert streamed.read_text() == mapped_verilog(ir, timing_only=True, timing_cell_types=types)
            finally:
                view.close()
        assert validate_timing_model_coverage(ir, model, types)["status"] == "pass"
        assert model["cells"]["CARRY8"]["delay_ns"] > 0.0
        assert model["cells"]["RAMB18E2"]["setup_ns"] > 0.0
        assert model["cells"]["RAMB18E2"]["clock_to_q_ns"] > 0.0
        assert model["cells"]["DSP48E2"]["delay_ns"] == 5.374
        assert types["ff"] == "FDRE__NEG"
        assert model["cells"]["FDRE__NEG"]["kind"] == "falling_edge_ff"
        assert model["source"]["qualification"] == "analytical_uncharacterized"


def test_cut_qualification_dispatches_xilinx_preplacement_timing_db():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        timing = root / "timing"
        timing.mkdir()
        intra = timing / "intrasite_delay_terms.txt"
        intra.write_text("""
bel A6LUT,B6LUT,C6LUT,D6LUT,E6LUT,F6LUT,G6LUT,H6LUT in SLICEM,SLICEL
A1 O6 150
bel A5LUT,B5LUT,C5LUT,D5LUT,E5LUT,F5LUT,G5LUT,H5LUT in SLICEM,SLICEL
A1 O5 165
bel F7MUX_AB,F7MUX_CD,F7MUX_EF,F7MUX_GH in SLICEM,SLICEL
S0 OUT 77
bel AFF,BFF,CFF,DFF,EFF,FFF,GFF,HFF,AFF2,BFF2,CFF2,DFF2,EFF2,FFF2,GFF2,HFF2 in SLICEM,SLICEL
CLK Q 77
CLK D -25
bel CARRY8 in SLICEM,SLICEL
CI O7 231
bel RAMB36E2
CLKARDCLK DOUTADOUT[0] 1003
ADDRARDADDR[0] CLKARDCLK 445
bel URAM288
CLK DOUT_A[0] 1200
CLK ADDR_A[0] 500
""", encoding="utf-8")
        inter = timing / "intersite_delay_terms.txt"
        inter.write_text(
            "SITEPIN_A1_DELAY 74\nIO_LONG 300\n", encoding="utf-8"
        )
        hashes = {
            "intersite_delay_terms.txt": _sha(inter),
            "intrasite_delay_terms.txt": _sha(intra),
        }
        database = root / "xilinx-preplacement.json"
        controls = root / "controls.json"
        controls.write_text(json.dumps(mock_ff_control_timing()))
        mapped = root / "mapped.json"
        _mapped_fixture(mapped)
        ir = import_yosys_json(mapped, top="top", clocks=["clk"])
        with patch.dict(
            "emuflow.xilinx_preplacement_timing.RAPIDWRIGHT_TIMING_DATA_SHA256",
            hashes,
            clear=True,
        ):
            build_xilinx_preplacement_timing_db(timing, database, ff_control_timing_path=controls)
            model, types = build_architecture_opensta_timing_model(ir, database)
            structural = classify_through_net_timing_endpoints(
                ir, model, [net["id"] for net in ir.value["nets"]], types
            )
            cut_net = next(
                net for net, record in structural.items()
                if record["status"] == "timed"
            )
            artifact = build_cut_segment_qualification_value(
                ir,
                {
                    "cut_nets": [{"net": cut_net}],
                    "semantic_contract": {
                        "cut_nodes": [{
                            "net": cut_net,
                            "source_segment_ids": ["segment000000"],
                            "capture_segment_ids": ["segment000001"],
                        }]
                    },
                },
                {"paths": []},
                architecture_timing_db_path=database,
            )
        assert artifact["status"] == "pass"
        assert artifact["summary"]["timed_structural_nets"] == 1
