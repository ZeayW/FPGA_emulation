#!/usr/bin/env python3
"""Small real-device qualification of the indexed Vivado provider.

Run from a versioned checkout with PYTHONPATH=src. All products belong to OUT;
this is a synthetic physical/provider check, not full-design global STA.
"""
import argparse
import csv
import json
from pathlib import Path

from emuflow.io import file_sha256, write_json
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.vivado_physical import run_indexed_vivado_partition
from emuflow.yosys import import_yosys_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--vivado", required=True)
    args = parser.parse_args()
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    ports = {name: {"direction": direction, "bits": bits} for name, direction, bits in (
        ("clk", "input", [2]), ("clk2", "input", [3]), ("fabric_clk", "input", [4]),
        ("d", "input", [5, 6]), ("q", "output", [8, 12]))}
    cells = {}
    for name, clock, data, output in (("launch", 2, 5, 7), ("capture", 2, 7, 8),
            ("launch2", 3, 6, 9), ("capture2", 3, 9, 10),
            ("fabric_launch", 4, 10, 11), ("fabric_capture", 4, 11, 12)):
        cells[name] = {"type": "FDRE", "parameters": {"INIT": "0"}, "attributes": {},
            "port_directions": {"C": "input", "D": "input", "Q": "output", "CE": "input", "R": "input"},
            "connections": {"C": [clock], "D": [data], "Q": [output], "CE": ["1"], "R": ["0"]}}
    source = root / "source.json"
    source.write_text(json.dumps({"modules": {"dut": {"attributes": {"top": "1"},
        "ports": ports, "cells": cells, "netnames": {f"n{n}": {"bits": [n]} for n in range(2, 13)}}}}))
    ir = root / "source.emuir.sqlite"
    import_yosys_json(source).save(ir)
    mapped = root / "dut.v"
    mapped_report = emit_vivado_mapped_verilog(ir, mapped)
    # Use real P/N identities from the repository's declared virtual board.
    board = Path(__file__).resolve().parents[2] / "platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.wires.csv"
    with board.open() as stream:
        rows = list(csv.DictReader(stream))[:4]
    adapter, xdc = root / "adapter.sv", root / "pins.xdc"
    rtl = ["module adapter(input wire [1:0] q, output wire [1:0] d,",
           " input wire rx0_p,rx0_n,rx1_p,rx1_n, output wire tx0_p,tx0_n,tx1_p,tx1_n);"]
    constraints, entries = [], []
    for row, (direction, bit) in zip(rows, (("rx", 0), ("rx", 1), ("tx", 0), ("tx", 1))):
        bus = "d" if direction == "rx" else "q"
        entry = {"direction": direction, "logical_port": bus, "logical_bit": bit,
                 "logical_port_width": 2, "iostandard": "LVDS"}
        for polarity in ("p", "n"):
            port, pin = f"{direction}{bit}_{polarity}", row[f"package_pin_a_{polarity}"]
            entry[polarity] = {"port": port, "package_pin": pin}
            constraints.extend((f"set_property PACKAGE_PIN {pin} [get_ports {{{port}}}]",
                                f"set_property IOSTANDARD LVDS [get_ports {{{port}}}]"))
        if direction == "rx":
            rtl.append(f"IBUFDS rx{bit}(.I(rx{bit}_p),.IB(rx{bit}_n),.O(d[{bit}]));")
        else:
            rtl.append(f"OBUFDS tx{bit}(.I(q[{bit}]),.O(tx{bit}_p),.OB(tx{bit}_n));")
        entries.append(entry)
    adapter.write_text("\n".join([*rtl, "endmodule", ""]))
    xdc.write_text("\n".join([*constraints, ""]))
    contract = {"status": "pass", "adapter": {"path": str(adapter), "sha256": file_sha256(adapter), "module": "adapter"},
        "xdc": {"path": str(xdc), "sha256": file_sha256(xdc)}, "entries": entries,
        "metrics": {"physical_ports": 8, "differential_pairs": 4}}
    runtime = {"fabric_clock": {"period_ns": 4.0},
        "virtual_dut_clock": {"nominal_period_ns": 20.0},
        "virtual_dut_clocks": {"clk": {"nominal_period_ns": 20.0}, "clk2": {"nominal_period_ns": 30.0}},
        "timing_model": {"dut_clock_ports": ["clk", "clk2"], "fabric_clock_port": "fabric_clk", "fabric_to_dut_max_delay_ns": 8.0}}
    report = run_indexed_vivado_partition(fpga="fpga0", part="xcvu19p-fsva3824-2-e", ir_path=ir,
        mapped_verilog_path=mapped, mapped_report=mapped_report, runtime=runtime,
        original_cells=6, transport_cells=0, output_dir=root / "physical", executable=args.vivado,
        virtual_io_contract=contract)
    write_json(root / "qualification.json", {"status": "pass", "scope": "synthetic-indexed-physical-only",
        "tool": report["tool"], "result": report["result"], "fixed_io": report["virtual_io_implementation"]})


if __name__ == "__main__":
    main()
