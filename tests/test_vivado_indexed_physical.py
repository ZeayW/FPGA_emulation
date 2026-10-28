import os
from contextlib import closing
import json
import shutil
import sqlite3
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from tests.vivado_input_host import prepare_vivado_inputs
from emuflow.physical.vivado.vivado_netlist import emit_vivado_mapped_verilog
from tests.vivado_measurement_host import write_vivado_local_query, import_vivado_local_timing
from emuflow.frontend.yosys import import_yosys_json


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
    def test_resource_inventory_counts_occupied_capacity_without_macro_wrappers(self):
        helper = Path(__file__).resolve().parents[1] / "scripts/vivado/resource_inventory.tcl"
        script = r'''
set cells {lut_macro lut5 lut6 packed5 packed6 dsp_macro dsp_alu dsp_mul ff carry ram18 ram36 uram io_macro diff ctrl}
set refs {LUT6_2 LUT5 LUT6 LUT5 LUT6 DSP48E2 DSP_ALU DSP_MULTIPLIER FDRE CARRY8 RAMB18E2 RAMB36E2 URAM288 IBUFDS DIFFINBUF IBUFCTRL}
set levels {MACRO LEAF LEAF LEAF LEAF MACRO INTERNAL INTERNAL LEAF LEAF LEAF LEAF LEAF MACRO INTERNAL INTERNAL}
foreach c $cells r $refs l $levels {
  dict set props $c REF_NAME $r
  dict set props $c PRIMITIVE_LEVEL $l
  dict set props $c EMUFLOW_MAPPED yes
  dict set props $c BEL A6LUT
  dict set sites $c "site_$c"
}
dict set sites lut5 slice0
dict set sites lut6 slice0
dict set props lut5 BEL SLICEM.A5LUT
dict set sites packed5 slice0
dict set sites packed6 slice0
dict set props packed5 BEL B5LUT
dict set props packed6 BEL SLICEL.B6LUT
dict set props packed5 EMUFLOW_MAPPED ""
dict set props packed6 EMUFLOW_MAPPED ""
dict set sites dsp_alu dsp0
dict set sites dsp_mul dsp0
proc get_cells {args} {global cells; return $cells}
proc get_sites {args} {global sites; return [dict get $sites [lindex $args end]]}
proc get_property {property object} {
  global props
  if {$property eq "NAME"} {return $object}
  return [dict get $props $object $property]
}
set result [emuflow_resource_inventory]
foreach {key expected} {lut 2 dsp48 1 ff 1 carry8 1 ramb18_cells 1 ramb36_cells 1 uram288 1 physical_cells 13 infrastructure_cells 2 routed_cells 9 optimization_cells 2} {
  if {[dict get $result $key] != $expected} {error "$key: [dict get $result $key] != $expected"}
}
dict set props lut5 BEL strange
if {![catch {emuflow_resource_inventory} message] || ![string match "unknown LUT BEL*" $message]} {error "bad LUT BEL accepted"}
dict set props lut5 BEL A5LUT
dict set sites dsp_mul {}
if {![catch {emuflow_resource_inventory} message] || ![string match "primitive lacks exactly one placed site*" $message]} {error "unplaced DSP accepted"}
dict set sites dsp_mul dsp0
dict set props dsp_mul PRIMITIVE_LEVEL UNKNOWN
if {![catch {emuflow_resource_inventory} message] || ![string match "unknown primitive level*" $message]} {error "unknown primitive level accepted"}
puts pass
'''
        result = subprocess.run([shutil.which("tclsh")], input=f"source {{{helper}}}\n" + script,
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "pass")

    def test_used_undriven_source_fails_before_mapping_with_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ir = fixture(root)
            with closing(sqlite3.connect(ir)) as db, db:
                net = db.execute("SELECT seq FROM net_records WHERE id='d'").fetchone()[0]
                db.execute("DELETE FROM endpoint_records WHERE net_seq=? AND role=0", (net,))
                db.execute("UPDATE net_records SET cut_class='undriven' WHERE seq=?", (net,))
            output = root / "new" / "dut.v"
            with patch("emuflow.physical.xilinx_netlist.emit_xilinx_mapped_index") as lower:
                with self.assertRaisesRegex(ValidationError, "1 used undriven"):
                    emit_vivado_mapped_verilog(ir, output)
                lower.assert_not_called()
            self.assertFalse(output.exists())
            report = json.loads(output.with_suffix(".source-check.json").read_text())
            self.assertEqual(report["source_declared_undriven_nets"], 1)
            self.assertEqual(report["examples"], [{"net": "d", "cut_class": "undriven"}])

    def test_unused_undriven_source_is_not_a_missing_used_driver(self):
        from emuflow.physical.vivado.vivado_source import validate_vivado_source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ir = fixture(root)
            with closing(sqlite3.connect(ir)) as db, db:
                net = db.execute("SELECT seq FROM net_records WHERE id='d'").fetchone()[0]
                db.execute("DELETE FROM endpoint_records WHERE net_seq=?", (net,))
                db.execute("UPDATE net_records SET cut_class='undriven' WHERE seq=?", (net,))
            self.assertEqual(validate_vivado_source(ir)["used_undriven_nets"], 0)

    def test_one_based_and_sparse_net_ordinals_do_not_alias_unused_ports(self):
        from emuflow.physical.xilinx_netlist import open_xilinx_mapped_index

        for sparse in (False, True):
            with self.subTest(sparse=sparse), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ir = fixture(root)
                with closing(sqlite3.connect(ir)) as db, db:
                    for table, column in (("net_records", "seq"), ("endpoint_records", "net_seq"), ("alias_records", "net_seq")):
                        db.execute(f"UPDATE {table} SET {column}={column}+100")
                        db.execute(f"UPDATE {table} SET {column}=" + (f"({column}-100)*10+10" if sparse else f"{column}-99"))
                    ports = [json.loads(row[0]) for row in db.execute("SELECT record_json FROM port_records")]
                    unused = {**next(p for p in ports if p["id"] == "d"), "id": "unused", "name": "unused"}
                    db.execute("INSERT INTO port_records VALUES (?,?)", (len(ports), json.dumps(unused)))
                    maximum = db.execute("SELECT max(seq) FROM net_records").fetchone()[0]
                report = emit_vivado_mapped_verilog(ir, root / "dut.v")
                with open_xilinx_mapped_index(Path(report["mapped_index"]["path"])) as mapped:
                    self.assertGreater(mapped.ports["unused"]["bits"][0], maximum + 2)
                text = (root / "dut.v").read_text()
                declarations = set(re.findall(r"wire (__emuflow_net_\d+);", text))
                references = set(re.findall(r"\b__emuflow_net_\d+\b", text))
                self.assertEqual(declarations, references)
                self.assertIn("`default_nettype none", text)

    def test_public_schema_accepts_native_resources_and_preserves_scope(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        from tests.test_physical_backend import PhysicalBackendContractTest

        result = PhysicalBackendContractTest()._result()
        result["identity"]["backend"] = "vivado"
        result["hard_resources"] = dict(lut=10, ff=90, bram18k=0, dsp48=0, carry8=0, uram288=0)
        result["resource_usage"] = {"physical": {"status": "pass"}}
        result["analysis_scope"] = {"implementation_mode": "out-of-context",
            "data_path_delay": "routed-vendor-device", "external_clock_sources": "abstract-unbound",
            "absolute_clock_insertion_and_skew": "not-qualified", "board_signoff": False}
        schema = Path(__file__).resolve().parents[1] / "schemas/physical-partition-result-v1.schema.json"
        validator = jsonschema.Draft202012Validator(json.loads(schema.read_text()))
        validator.validate(result)
        result["analysis_scope"]["board_signoff"] = True
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(result)
        result["analysis_scope"]["board_signoff"] = False
        result["hard_resources"]["ff"] = -1
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(result)

    def test_native_sequential_controls_and_bram_physical_aliases(self):
        from tests.physical_query_reference import _vivado_object

        ir = SimpleNamespace(value={})
        for kind, control in (("FDRE", "R"), ("FDSE", "S"), ("FDCE", "CLR"), ("FDPE", "PRE")):
            instances = {"ff": {"type": kind}}
            pins = {"ff": {("CE", 0), (control, 0)}}
            for port in ("CE", control):
                self.assertEqual(_vivado_object(ir, {"instance": "ff", "port": port, "bit": 0}, pins, instances),
                                 ("pin", f"ff/{port}"))
        self.assertEqual(_vivado_object(ir, {"instance": "bram", "port": "WEA", "bit": 2},
                         {"bram": {("WEA", 0), ("WEA", 1)}}, {"bram": {"type": "RAMB18E2"}}),
                         ("pin", "bram/WEA[0]"))

    def test_indexed_boundary_import_preserves_board_scope_and_exact_coverage(self):
        from emuflow.timing.boundary_identity_storage import write_boundary_identity_storage
        from emuflow.timing.boundary_timing_storage import read_boundary_timing_storage
        from tests.vivado_measurement_host import import_indexed_vivado_boundary
        def _header():
            return {"schema": "emuflow.boundary-identity/v1", "status": "pass",
                    "design": "dut", "platform": "board", "fpga": "fpga0", "provider": "test"}

        def _record(identity, kind, lane):
            return {"id": identity, "kind": kind, "schedule_entry": f"s{lane}",
                "demand": f"d{lane}", "logical_net": f"n{lane}", "link": "link", "peer": "fpga1",
                "lane": lane, "logical_lane": lane, "internal_signal": f"signal{lane}",
                "source_class": "dut-net" if kind == "tx" else "captured-shadow",
                "merged_ir": {"external_port": kind, "external_port_bit": lane,
                    "external_net": f"wire{lane}", "logical_net": f"n{lane}",
                    "boundary_register_instances": [] if kind == "tx" else ["shadow"]}}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.json"
            with write_boundary_identity_storage(identity, _header(), [_record("tx", "tx", 0), _record("rx", "rx", 1)]):
                pass
            measured = root / "measured.tsv"
            header = "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\n"
            h = lambda text: text.encode().hex()
            rows = ["\t".join((h(kind), kind, "1.25", h("start"), h("end"))) + "\n" for kind in ("tx", "rx")]
            measured.write_text(header + "".join(rows))
            output = root / "timing.json"
            original_read_text = Path.read_text
            def reject_whole_measurement_read(path, *args, **kwargs):
                if path == measured:
                    raise AssertionError("boundary timing TSV was materialized")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", reject_whole_measurement_read):
                report = import_indexed_vivado_boundary(
                    measured, identity, output, measurement_scope="board-integrated-interface",
                    provider="vivado-board-integrated-interface-path-v1",
                    qualification="routed-board-integrated-interface-path",
                )
            self.assertEqual(report["endpoints"], 2)
            with read_boundary_timing_storage(output) as view:
                self.assertEqual(view["measurement_scope"], "board-integrated-interface")
                self.assertEqual(view["provider"], "vivado-board-integrated-interface-path-v1")
                self.assertEqual(view["qualification"], "routed-board-integrated-interface-path")
                self.assertEqual(view["endpoints"][0]["measurement"],
                                 "interface-startpoint-through-rx-boundary-to-shadow-capture")
            for bad in (rows[:1], rows + rows[:1], [rows[0].replace("1.25", "nan"), rows[1]],
                        [rows[0].replace(h("tx"), h("foreign"), 1), rows[1]]):
                measured.write_text(header + "".join(bad))
                with self.assertRaises(ValidationError):
                    import_indexed_vivado_boundary(measured, identity, root / "bad.json")
                self.assertFalse((root / "bad.json").exists())

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
        from tests.physical_query_reference import _vivado_object

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
            with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("whole graph")):
                report = emit_vivado_mapped_verilog(ir, root / "dut.v")
                inputs = prepare_vivado_inputs(ir, root, {
                    "fabric_clock": {"period_ns": 4.0},
                    "virtual_dut_clocks": {"clk": {"nominal_period_ns": 20.0}},
                    "timing_model": {"dut_clock_ports": ["clk"], "fabric_clock_port": "fabric_clk", "fabric_to_dut_max_delay_ns": 8.0}})
                xdc = Path(inputs["runtime_xdc"]).read_text()
            self.assertEqual(report["emitted_instances"], 2)
            self.assertTrue(Path(report["mapped_index"]["path"]).is_file())
            self.assertIn("emuflow_dut_clk_0", xdc)
            text = (root / "dut.v").read_text()
            self.assertIn("\\FDRE", text)
            self.assertIn("1'b1", text)
            self.assertNotIn("LOC =", text)
            self.assertNotIn("KEEP =", text)
            self.assertNotIn("DONT_TOUCH", text)
            self.assertIn('EMUFLOW_SOURCE = "s', text)


    def test_complete_local_paths_use_indexed_queries_and_reject_corruption(self):
        from tests.native_build import emuflow_native
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"EMUFLOW_NATIVE_ROOT": str(emuflow_native().parent.parent)}):
            root = Path(directory)
            from tests.local_path_query_fixture import sources, prepare
            from tests.native_build import emuflow_native
            with patch.dict(os.environ, {'EMUFLOW_NATIVE_ROOT': str(emuflow_native().parent.parent)}):
                case = sources(root/'sources', paths=[
                    ('launch/Q->capture/D#00000000','clk',20.0,18.0,2.0,['n0','n1'])])
                prepared = prepare(case)
                with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("whole graph")):
                    query_report = write_vivado_local_query(case.merged['fpga0'], "fpga0",
                        root/'query.tsv', root/'identity.json', prepared)
            identity = Path(query_report['identity'])
            header = "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\tmeasurement\tactual_start_object_hex\tactual_end_object_hex\n"
            h = lambda text: text.encode().hex()
            row = "\t".join((h("launch/Q->capture/D#00000000"), "local", "2.5", h("launch/Q"), h("capture/D"), "endpoint-exact", h("launch/Q"), h("capture/D"))) + "\n"
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
                             "p": {"port": "link_p", "package_pin": "P1"}, "n": {"port": "link_n", "package_pin": "P2"}, "iostandard": "LVDS"}]}
            from tests.test_vivado_backend import RUNTIME
            inputs = prepare_vivado_inputs(ir, root, RUNTIME, contract)
            report = inputs["fixed_io"]["top"]
            self.assertEqual(report["hierarchy_prefix"], "mapped_partition")
            self.assertNotIn("output wire \\q ", Path(report["output"]).read_text())
            xdc.write_text("# changed\n")
            with self.assertRaises(ValidationError):
                prepare_vivado_inputs(ir, root, RUNTIME, contract)


if __name__ == "__main__":
    unittest.main()
