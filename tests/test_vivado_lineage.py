from contextlib import closing
import csv
import json
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.common.errors import ValidationError
from emuflow.physical.vivado.vivado_lineage import (INVENTORY_HEADER, LINEAGE_POLICY,
                                    PLACEMENT_COORDINATE_SYSTEM)
from tests.vivado_lineage_host import import_cell_lineage
from tests.vivado_query_host import resolve_vivado_query as resolve_timing_query
from emuflow_tools.physical.vivado.vivado_netlist import emit_vivado_mapped_verilog
from tests.yosys_reference import import_yosys_json


class VivadoLineageTests(unittest.TestCase):
    def test_large_census_streams_and_releases_pre_route_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report,mapped,functions,inventory = self.prepare(root)
            with closing(sqlite3.connect(mapped)) as connection, connection:
                origin = connection.execute("SELECT seq FROM cells WHERE id='capture'").fetchone()[0]
            before = root/"before.tsv"
            with before.open("w") as stream:
                writer = csv.writer(stream,delimiter="\t",lineterminator="\n",quoting=csv.QUOTE_NONE)
                writer.writerow(INVENTORY_HEADER)
                for index in range(100000):
                    writer.writerow([f"partition/long_repeated_source_name/replica_{index:06d}","FDRE",f"s{origin}","", "", "", "", "", ""])
            output = root/"lineage.sqlite"
            with patch.object(Path,"read_text",side_effect=AssertionError("whole inventory read")), patch(
                "csv.reader", side_effect=AssertionError("Python census parser called")
            ):
                summary = import_cell_lineage(mapped,functions,{"synthesized":before,"routed":inventory},output,
                                              mapped_sha256=report["mapped_index"]["sha256"])
            self.assertEqual(summary["stages"]["synthesized"]["observed_objects"],100000)
            with closing(sqlite3.connect(output)) as connection, connection:
                self.assertEqual(connection.execute("SELECT DISTINCT stage FROM objects").fetchall(),[("routed",)])
                self.assertEqual(connection.execute("SELECT count(*) FROM objects").fetchone()[0],3)

    @unittest.skipUnless(shutil.which("tclsh"), "requires Tcl interpreter")
    def test_inventory_preserves_raw_quotes_and_backslashes_once(self):
        helper = Path(__file__).resolve().parents[1]/"scripts/vivado/source_inventory.tcl"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root/"inventory.tsv"
            mock = r'''
if {[catch {binary encode hex a}]} {
  rename binary native_binary
  proc binary {operation format value} {native_binary scan $value H* result; return $result}
}
proc get_cells {args} {return {a}}
proc get_property {args} {
  global props
  set key [lindex $args end-1]
  if {[dict exists $props $key]} {return [dict get $props $key]}
  return ""
}
dict set props NAME {"quoted\path[0]}
dict set props EMUFLOW_SOURCE s0
dict set props REF_NAME FDRE
'''
            wrapper = root/"run.tcl"
            wrapper.write_text(mock+f"\nsource {{{helper}}}\nemuflow_inventory {{{output}}} {{}}\n")
            completed = subprocess.run([shutil.which("tclsh"),str(wrapper)],capture_output=True,text=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            with output.open() as stream:
                rows = list(csv.reader(stream,delimiter="\t",quoting=csv.QUOTE_NONE))
            self.assertEqual(rows[0],INVENTORY_HEADER)
            self.assertEqual(rows[1][:3],['"quoted\\path[0]',"FDRE","s0"])

    @unittest.skipUnless(shutil.which("tclsh"), "requires Tcl interpreter")
    def test_export_queries_all_descendants_and_checks_actual_extent(self):
        helper = Path(__file__).resolve().parents[1]/"scripts/vivado/export_logic_segment_timing.tcl"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query, output = root/"query.tsv", root/"timing.tsv"
            query.write_text("endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex\tresolved_start_pins_hex\tresolved_end_pins_hex\tresolved_cone_pins_hex\n"+
                "\t".join(["70","local","pin","source/Q".encode().hex(),"pin","sink/D".encode().hex(),"pin","unused/O".encode().hex(),
                             ",".join(value.encode().hex() for value in ("a with space/Q","b with space/Q")),"y/D".encode().hex(),""])+"\n")
            mock = r'''
# macOS ships Tcl 8.5; emulate only the codec used by the Vivado 8.6 script.
if {[catch {binary decode hex 61}]} {
  rename binary native_binary
  proc binary {operation format value} {
    if {$format ne "hex"} {error "unexpected codec"}
    if {$operation eq "decode"} {return [native_binary format H* $value]}
    if {$operation eq "encode"} {native_binary scan $value H* result; return $result}
    error "unexpected codec operation"
  }
}
proc open_checkpoint {args} {}
proc get_clocks {args} {return {}}
proc get_cells {args} {return {a b}}
proc get_pins {args} {
  if {[string match *unused/O* $args]} {error "unused cone was queried"}
  if {[lsearch -exact $args -of_objects] >= 0} {return {a/C b/C}}
  return [lindex $args end]
}
proc get_property {key object} {
  global actual_end
  switch $key {
    NAME {
      if {[llength $object] == 1} {return [lindex $object 0]}
      return $object
    }
    CLASS {return [lrepeat [llength $object] pin]}
    STARTPOINT_PIN {return b/C}
    ENDPOINT_PIN {return $actual_end}
    DATAPATH_DELAY {return 4.25}
    default {error "unexpected property $key"}
  }
}
proc get_timing_paths {args} {
  set from [lindex $args [expr {[lsearch -exact $args -from]+1}]]
  set to [lindex $args [expr {[lsearch -exact $args -to]+1}]]
  if {$from ne {{a with space/Q} {b with space/Q}} || $to ne {y/D}} {error "descendant collection was truncated or nested"}
  return path
}
'''
            wrapper = root/"run.tcl"
            for end, success in (("y/D", True), ("z/D", False)):
                wrapper.write_text(f"set actual_end {end}\nset argc 3\nset argv [list checkpoint {{{query}}} {{{output}}}]\n"+mock+f"\nsource {{{helper}}}\n")
                completed = subprocess.run([shutil.which("tclsh"),str(wrapper)],text=True,capture_output=True)
                self.assertEqual(completed.returncode==0,success,completed.stderr)
                if success:
                    self.assertIn("4.25", output.read_text())
                else:
                    self.assertIn("suffix beyond requested endpoint",completed.stderr)

    def prepare(self, root, *, alias_init="0", prefix=""):
        cells = {}
        for name, data, output in (("launch", 4, 5), ("capture", 5, 6), ("alias", 5, 7)):
            cells[name] = {"type": "FDRE", "parameters": {"INIT": alias_init if name=="alias" else "0"},
                "attributes": {}, "port_directions": {"C":"input", "D":"input", "CE":"input", "R":"input", "Q":"output"},
                "connections": {"C":[2], "D":[data], "CE":["1"], "R":["0"], "Q":[output]}}
        cells = {prefix+name: cell for name,cell in cells.items()}
        value = {"modules":{"dut":{"attributes":{"top":"1"}, "cells":cells,
            "ports":{"clk":{"direction":"input","bits":[2]}, "d":{"direction":"input","bits":[4]},
                     "q":{"direction":"output","bits":[6,7]}},
            "netnames":{f"n{i}":{"bits":[i]} for i in (2,4,5,6,7)}}}}
        source = root/"source.json"
        source.write_text(json.dumps(value))
        ir = root/"source.emuir.sqlite"
        import_yosys_json(source).save(ir)
        report = emit_vivado_mapped_verilog(ir, root/"dut.v")
        self.assertEqual(report["optimization_policy"], LINEAGE_POLICY)
        self.assertNotIn("DONT_TOUCH", (root/"dut.v").read_text())
        mapped, functions = Path(report["mapped_index"]["path"]), Path(report["source_functions"]["path"])
        with closing(sqlite3.connect(mapped)) as connection, connection:
            origins = dict(connection.execute("SELECT id,seq FROM cells"))
        inventory = root/"routed.tsv"
        self.write_inventory(inventory, [
            ("partition/renamed_launch", "renamed_launch", "FDRE", f"s{origins[prefix+'launch']}"),
            ("partition/renamed_capture", "renamed_capture", "FDRE", f"s{origins[prefix+'capture']}"),
            ("partition/replica", "replica", "FDRE", f"s{origins[prefix+'capture']}"),
        ])
        return report, mapped, functions, inventory

    def write_inventory(self, path, rows):
        with path.open("w") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE)
            writer.writerow(INVENTORY_HEADER)
            for index, (name, relative, ref, origin) in enumerate(rows):
                writer.writerow([name, ref, origin, "", f"SLICE_X{index}Y0", "AFF", str(index), "0", "LEAF"])

    def test_merge_and_replication_keep_original_query_population(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, mapped, functions, inventory = self.prepare(root)
            lineage = root/"lineage.sqlite"
            summary = import_cell_lineage(mapped, functions, {"routed":inventory}, lineage,
                                           mapped_sha256=report["mapped_index"]["sha256"])
            counts = summary["stages"]["routed"]
            self.assertEqual(counts["source_cells_with_lineage"], 2)
            self.assertEqual(counts["unretained_source_cells"], 1)
            self.assertEqual(counts["source_cells_with_multiple_objects"], 1)
            query = root/"query.tsv"
            header = "endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex"
            query.write_text(header+"\n"+"\t".join(["70","local","pin","launch/Q".encode().hex(),"pin","alias/D".encode().hex(),"",""])+"\n")
            output = root/"resolved.tsv"
            resolution = resolve_timing_query(mapped,functions,lineage,query,output)
            self.assertEqual(resolution["queries"],1)
            with Path(resolution["output"]).open() as stream:
                rows = list(csv.reader(stream,delimiter="\t"))
            self.assertEqual(rows[1][5], "alias/D".encode().hex())
            self.assertEqual(bytes.fromhex(rows[1][8]).decode(), "partition/renamed_launch/Q")
            self.assertEqual({bytes.fromhex(value).decode() for value in rows[1][9].split(",")},
                             {"partition/renamed_capture/D", "partition/replica/D"})

    def test_same_inputs_with_different_initial_state_do_not_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, mapped, functions, inventory = self.prepare(root, alias_init="1")
            lineage = root/"lineage.sqlite"
            import_cell_lineage(mapped,functions,{"routed":inventory},lineage,mapped_sha256=report["mapped_index"]["sha256"])
            query = root/"query.tsv"
            query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n70\trx\t71\t0\t6e\t"+"alias".encode().hex()+"\n")
            with self.assertRaisesRegex(ValidationError, "no proven endpoint association"):
                resolve_timing_query(mapped,functions,lineage,query,root/"resolved.tsv",boundary=True)

    def test_retained_origin_is_not_broadened_to_other_equivalent_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report,mapped,functions,inventory = self.prepare(root)
            with closing(sqlite3.connect(mapped)) as connection, connection:
                origin = connection.execute("SELECT seq FROM cells WHERE id='alias'").fetchone()[0]
            with inventory.open("a") as stream:
                csv.writer(stream,delimiter="\t",lineterminator="\n").writerow([
                    "partition/alias","FDRE",f"s{origin}","", "SLICE_X3Y0", "AFF", "3", "0", "LEAF"])
            lineage = root/"lineage.sqlite"
            import_cell_lineage(mapped,functions,{"routed":inventory},lineage,mapped_sha256=report["mapped_index"]["sha256"])
            query = root/"query.tsv"
            query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n70\trx\t71\t0\t6e\t"+"alias".encode().hex()+"\n")
            output = root/"resolved.tsv"
            resolution = resolve_timing_query(mapped,functions,lineage,query,output,boundary=True)
            with Path(resolution["output"]).open() as stream:
                rows = list(csv.reader(stream,delimiter="\t"))
            self.assertEqual(bytes.fromhex(rows[1][-1]).decode(),"partition/alias")

    def test_unused_missing_cone_does_not_reject_mandatory_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, mapped, functions, inventory = self.prepare(root)
            lineage = root / "lineage.sqlite"
            import_cell_lineage(mapped, functions, {"routed": inventory}, lineage,
                                mapped_sha256=report["mapped_index"]["sha256"])
            header = ["endpoint_hex", "kind", "start_kind", "start_object_hex",
                      "end_kind", "end_object_hex", "cone_anchor_kind", "cone_anchor_object_hex"]
            row = ["70", "local", "pin", "launch/Q".encode().hex(), "pin",
                   "capture/D".encode().hex(), "pin", "removed/O".encode().hex()]
            query, output = root / "query.tsv", root / "resolved.tsv"
            with query.open("w") as stream:
                csv.writer(stream, delimiter="\t", lineterminator="\n").writerows([header, row])
            resolution = resolve_timing_query(mapped, functions, lineage, query, output)
            self.assertEqual(resolution["queries"], 1)
            with Path(resolution["output"]).open() as stream:
                resolved = list(csv.reader(stream, delimiter="\t"))
            self.assertEqual(resolved[1][:8], row)
            self.assertTrue(resolved[1][8])
            self.assertTrue(resolved[1][9])
            self.assertEqual(resolved[1][10], "")
            row[5] = "removed/D".encode().hex()
            with query.open("w") as stream:
                csv.writer(stream, delimiter="\t", lineterminator="\n").writerows([header, row])
            with self.assertRaisesRegex(ValidationError, "no proven endpoint association"):
                resolve_timing_query(mapped, functions, lineage, query, output)

    def test_invalid_duplicate_unknown_and_mixed_source_inventories_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report,mapped,functions,inventory = self.prepare(root)
            original = inventory.read_text()
            for case, text in enumerate((original+original.splitlines()[1]+"\n", original.replace("FDRE\ts", "FDRE\tbad"),
                                         original.replace("\ts", "\ts999999"))):
                inventory.write_text(text)
                with self.assertRaises(ValidationError):
                    import_cell_lineage(mapped,functions,{"routed":inventory},root/f"bad{case}.sqlite",
                                        mapped_sha256=report["mapped_index"]["sha256"])
            inventory.write_text(original)
            with self.assertRaisesRegex(ValidationError,"different mapped input"):
                import_cell_lineage(mapped,functions,{"routed":inventory},root/"mixed.sqlite",mapped_sha256="0"*64)

    def test_retimed_descendant_cannot_be_silently_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report,mapped,functions,inventory = self.prepare(root)
            with inventory.open() as stream:
                rows = list(csv.reader(stream,delimiter="\t"))
            rows[-1][3] = "RETIMING".encode().hex()
            with inventory.open("w") as stream:
                csv.writer(stream,delimiter="\t",lineterminator="\n").writerows(rows)
            lineage = root/"lineage.sqlite"
            import_cell_lineage(mapped,functions,{"routed":inventory},lineage,mapped_sha256=report["mapped_index"]["sha256"])
            query = root/"query.tsv"
            query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n70\trx\t71\t0\t6e\t"+"alias".encode().hex()+"\n")
            with self.assertRaisesRegex(ValidationError,"unsupported sequential transformation"):
                resolve_timing_query(mapped,functions,lineage,query,root/"resolved.tsv",boundary=True)

    def test_vivado_escaped_source_name_uses_indexed_primary_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report,mapped,functions,inventory = self.prepare(root,prefix="escaped\\")
            lineage = root/"lineage.sqlite"
            import_cell_lineage(mapped,functions,{"routed":inventory},lineage,mapped_sha256=report["mapped_index"]["sha256"])
            query = root/"query.tsv"
            query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n70\trx\t71\t0\t6e\t"+"escaped\\\\alias".encode().hex()+"\n")
            self.assertEqual(resolve_timing_query(mapped,functions,lineage,query,root/"resolved.tsv",boundary=True)["queries"],1)

    def test_native_resolution_preserves_all_sorted_descendants_and_port_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, mapped, functions, inventory = self.prepare(root, prefix="escaped\\路径/")
            lineage = root/"lineage.sqlite"
            import_cell_lineage(mapped, functions, {"routed": inventory}, lineage,
                                mapped_sha256=report["mapped_index"]["sha256"])
            query, requested = root/"query.tsv", root/"resolved.tsv"
            header = ["endpoint_hex", "kind", "start_kind", "start_object_hex", "end_kind",
                      "end_object_hex", "cone_anchor_kind", "cone_anchor_object_hex"]
            name = "escaped\\\\路径/alias/D"
            rows = [["70", "local", "port", "d".encode().hex(), "pin", name.encode().hex(), "", ""]]
            with query.open("w") as stream:
                csv.writer(stream, delimiter="\t", lineterminator="\n").writerows([header, *rows])
            requested.write_text("prior generation\n")
            with patch("csv.reader", side_effect=AssertionError("Python query row parser called")), patch(
                    "sqlite3.connect", side_effect=AssertionError("Python descendant query called")):
                resolution = resolve_timing_query(mapped, functions, lineage, query, requested)
            self.assertEqual(requested.read_text(), "prior generation\n")
            with Path(resolution["output"]).open() as stream:
                actual = list(csv.reader(stream, delimiter="\t"))
            self.assertEqual(actual[1][:8], rows[0])
            self.assertEqual(actual[1][8], "")  # A top port never becomes a descendant cell.
            self.assertEqual([bytes.fromhex(value).decode() for value in actual[1][9].split(",")],
                             ["partition/renamed_capture/D", "partition/replica/D"])
            query.write_text("\t".join(header)+"\n"+"\t".join(rows[0][:-1])+"\n")
            with self.assertRaises(ValidationError):
                resolve_timing_query(mapped, functions, lineage, query, requested)
            self.assertEqual(requested.read_text(), "prior generation\n")
            with query.open("w") as stream:
                csv.writer(stream, delimiter="\t", lineterminator="\n").writerows([header, rows[0], rows[0]])
            with self.assertRaises(ValidationError):
                resolve_timing_query(mapped, functions, lineage, query, requested)
            with self.assertRaisesRegex(ValidationError, "aliases"):
                resolve_timing_query(mapped, functions, lineage, query, functions)
            self.assertEqual(len(list(root.glob("resolved.tsv.generation.*"))), 1)

    def test_native_resolution_rejects_mixed_owner_and_nonrouted_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, mapped, functions, inventory = self.prepare(root)
            lineage = root/"lineage.sqlite"
            import_cell_lineage(mapped, functions, {"routed": inventory}, lineage,
                                mapped_sha256=report["mapped_index"]["sha256"])
            query, requested = root/"query.tsv", root/"resolved.tsv"
            query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n70\trx\t71\t0\t6e\t"+"alias".encode().hex()+"\n")
            with closing(sqlite3.connect(functions)) as db, db:
                db.execute("UPDATE metadata SET value=? WHERE key='mapped_index_sha256'", ("0"*64,))
            with self.assertRaisesRegex(ValidationError, "different mapped input"):
                resolve_timing_query(mapped, functions, lineage, query, requested, boundary=True)
            with closing(sqlite3.connect(functions)) as db, db:
                db.execute("UPDATE metadata SET value=? WHERE key='mapped_index_sha256'", (report["mapped_index"]["sha256"],))
            with closing(sqlite3.connect(lineage)) as db, db:
                db.execute("UPDATE metadata SET value='placed' WHERE key='retained_stage'")
            with self.assertRaisesRegex(ValidationError, "routed stage"):
                resolve_timing_query(mapped, functions, lineage, query, requested, boundary=True)
            self.assertFalse(requested.exists())
            self.assertEqual(list(root.glob("resolved.tsv.generation.*")), [])
