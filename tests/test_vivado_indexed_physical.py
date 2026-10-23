import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.vivado_backend import vivado_runtime_xdc
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.vivado_physical import parse_timing_summary, validate_indexed_cell_coverage
from emuflow.vivado_physical_timing import write_vivado_local_query, import_vivado_local_timing
from emuflow.vivado_physical_top import write_vivado_physical_top
from emuflow.yosys import import_yosys_json


def fixture(root):
    value = {"creator": "test", "modules": {"dut": {
        "attributes": {"top": "1"},
        "ports": {"clk": {"direction": "input", "bits": [2]},
                  "fabric_clk": {"direction": "input", "bits": [3]},
                  "d": {"direction": "input", "bits": [4]},
                  "q": {"direction": "output", "bits": [6]}},
        "cells": {name: {"type": "FDRE", "parameters": {"INIT": "0"}, "attributes": {},
            "port_directions": {"C": "input", "D": "input", "Q": "output", "CE": "input", "R": "input"},
            "connections": {"C": [2], "D": [d], "Q": [q], "CE": ["1"], "R": ["0"]}}
            for name, d, q in (("launch", 4, 5), ("capture", 5, 6))},
        "netnames": {name: {"bits": [bit]} for name, bit in (("clk", 2), ("fabric_clk", 3), ("d", 4), ("middle", 5), ("q", 6))},
    }}}
    source = root / "source.json"
    source.write_text(json.dumps(value))
    ir = root / "source.emuir.sqlite"
    import_yosys_json(source).save(ir)
    return ir


class IndexedVivadoTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("tclsh"), "requires Tcl interpreter")
    def test_timing_extent_rejects_enclosing_prefix_and_suffix(self):
        helper = Path(__file__).resolve().parents[1] / "scripts/vivado/timing_contract.tcl"
        script = r'''
proc get_property {property object} {
  global actual_start actual_end
  if {$object eq "path"} {
    if {$property eq "STARTPOINT_PIN"} {return $actual_start}
    if {$property eq "ENDPOINT_PIN"} {return $actual_end}
  }
  if {$property eq "CLASS"} {return pin}
  return $object
}
proc get_cells {args} {return launch}
proc get_pins {args} {return launch/C}
set actual_start launch/C
set actual_end capture/D
emuflow_validate_segment_extent path launch/Q capture/D endpoint-exact
set actual_start foreign/C
if {![catch {emuflow_validate_segment_extent path launch/Q capture/D endpoint-exact}]} {error "accepted prefix"}
emuflow_validate_segment_extent path launch/Q capture/D cut-net-cone-upper-bound
set actual_end suffix/D
if {![catch {emuflow_validate_segment_extent path launch/Q capture/D cut-net-cone-upper-bound}]} {error "accepted suffix"}
puts pass
'''
        completed = subprocess.run([shutil.which("tclsh")], input=f"source {{{helper}}}\n" + script,
                                   text=True, capture_output=True, check=True)
        self.assertEqual(completed.stderr, "")
        self.assertIn("pass", completed.stdout)

    def test_native_dual_output_lut_preserves_output_pin_identity(self):
        from emuflow.logic_segment_timing import _vivado_object

        ir = SimpleNamespace(value={})
        instances = {"carry_lut": {"type": "LUT6_2"}}
        pins = {"carry_lut": {("O5", 0), ("O6", 0), ("I0", 0)}}
        for port in ("O5", "O6", "I0"):
            self.assertEqual(_vivado_object(ir, {"instance": "carry_lut", "port": port, "bit": 0}, pins, instances),
                             ("pin", f"carry_lut/{port}"))

    def test_native_export_and_runtime_never_load_whole_emuir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ir = fixture(root)
            with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("whole graph")), patch("emuflow.vivado_netlist.lower_vivado_primitives", side_effect=AssertionError("deep copy")):
                report = emit_vivado_mapped_verilog(ir, root / "dut.v")
                xdc = vivado_runtime_xdc(ir, {
                    "fabric_clock": {"period_ns": 4.0},
                    "virtual_dut_clocks": {"clk": {"nominal_period_ns": 20.0}},
                    "timing_model": {"dut_clock_ports": ["clk"], "fabric_clock_port": "fabric_clk", "fabric_to_dut_max_delay_ns": 8.0}})
            self.assertEqual(report["emitted_instances"], 2)
            self.assertTrue(Path(report["mapped_index"]["path"]).is_file())
            self.assertIn("emuflow_dut_clk_0", xdc)
            text = (root / "dut.v").read_text()
            self.assertIn("\\FDRE", text)
            self.assertIn("1'b1", text)
            self.assertNotIn("LOC =", text)

    def test_cell_inventory_rejects_missing_duplicate_and_wrong_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = emit_vivado_mapped_verilog(fixture(root), root / "dut.v")
            index = Path(report["mapped_index"]["path"])
            inventory = root / "inventory.tsv"
            inventory.write_text("name\tref_name\nlaunch\tFDRE\ncapture\tFDRE\n")
            self.assertEqual(validate_indexed_cell_coverage(index, {"routed": inventory})["logical_cells"], 2)
            for rows in ("launch\tFDRE\n", "launch\tFDRE\nlaunch\tFDRE\n", "launch\tLUT1\ncapture\tFDRE\n"):
                with self.subTest(rows=rows):
                    inventory.write_text("name\tref_name\n" + rows)
                    with self.assertRaises(ValidationError):
                        validate_indexed_cell_coverage(index, {"routed": inventory})

    def test_endpoint_aggregate_accepts_negative_slack_and_rejects_bad_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timing.rpt"
            path.write_text("Design Timing Summary\n WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints\n-----\n-0.125 -0.250 2 10 0.1 0.0 0 10\n")
            self.assertEqual(parse_timing_summary(path)["tns_ns"], -0.25)
            path.write_text(path.read_text().replace("2 10", "11 10"))
            with self.assertRaises(ValidationError):
                parse_timing_summary(path)

    def test_complete_local_paths_use_indexed_queries_and_reject_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ir = fixture(root)
            source = {name: "0" * 64 for name in ("path_database_sha256", "original_ir_sha256", "assignment_sha256", "routes_sha256", "original_path_ids_sha256")}
            source["original_paths"] = 1
            prepared = SimpleNamespace(assignment={"design": "dut"}, source_manifest=source,
                local_paths_by_fpga={"fpga0": ({"id": "path0", "clock_domain": "clk", "clock_period_ns": 20.0,
                    "fixed_delay_ns": 2.0, "slack_ns": 18.0,
                    "startpoint": {"instance": "launch", "port": "Q", "bit": 0},
                    "endpoint": {"instance": "capture", "port": "D", "bit": 0}},)})
            identity = root / "identity.sqlite"
            query = root / "query.tsv"
            with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("whole graph")):
                write_vivado_local_query(ir, "fpga0", query, identity, prepared)
            header = "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\tmeasurement\tactual_start_object_hex\tactual_end_object_hex\n"
            h = lambda text: text.encode().hex()
            row = "\t".join((h("path0"), "local", "2.5", h("launch/Q"), h("capture/D"), "endpoint-exact", h("launch/Q"), h("capture/D"))) + "\n"
            measured = root / "measured.tsv"
            measured.write_text(header + row)
            report = import_vivado_local_timing(measured, identity, root / "timing.json")
            self.assertEqual(report["local_paths"], 1)
            for rows in ("", row + row, row.replace(h("capture/D"), h("capture/Q")), row.replace("2.5", "nan")):
                with self.subTest(rows=rows):
                    measured.write_text(header + rows)
                    with self.assertRaises(ValidationError):
                        import_vivado_local_timing(measured, identity, root / "bad.json")

    def test_fixed_io_top_consumes_sealed_adapter_and_rejects_stale_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ir = fixture(root)
            adapter, xdc = root / "adapter.sv", root / "pins.xdc"
            adapter.write_text("module adapter(input wire [0:0] q, output wire p,n); endmodule\n")
            xdc.write_text("# fixed pins\n")
            contract = {"status": "pass", "adapter": {"path": str(adapter), "sha256": file_sha256(adapter), "module": "adapter"},
                "xdc": {"path": str(xdc), "sha256": file_sha256(xdc)}, "metrics": {"physical_ports": 2},
                "entries": [{"direction": "tx", "logical_port": "q", "logical_bit": 0, "logical_port_width": 1,
                             "p": {"port": "link_p"}, "n": {"port": "link_n"}}]}
            report = write_vivado_physical_top(ir, root / "top.sv", contract)
            self.assertEqual(report["hierarchy_prefix"], "mapped_partition")
            self.assertNotIn("output wire \\q ", (root / "top.sv").read_text())
            xdc.write_text("# changed\n")
            with self.assertRaises(ValidationError):
                write_vivado_physical_top(ir, root / "bad.sv", contract)


if __name__ == "__main__":
    unittest.main()
