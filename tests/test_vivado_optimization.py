from contextlib import closing
import csv
import json
import sqlite3
import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.vivado_lineage import INVENTORY_HEADER, import_cell_lineage, resolve_timing_query
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.vivado_optimization import build_optimization_proofs, OptimizationProofView
from emuflow.yosys import import_yosys_json


class VivadoOptimizationTests(unittest.TestCase):
    def prepare(self, root, *, stage="routed", empty=False):
        cells = {}

        def register(name, q, d, *, init="0", ce="1", reset="0", kind="FDRE", parameters=None):
            control = {"FDRE": "R", "FDSE": "S", "FDCE": "CLR", "FDPE": "PRE"}[kind]
            cells[name] = {"type": kind, "parameters": {"INIT": init, **(parameters or {})},
                           "attributes": {}, "port_directions": {"C":"input", "CE":"input", "D":"input",control:"input", "Q":"output"},
                           "connections": {"C":[2], "CE":[ce], "D":[d], control:[reset], "Q":[q]}}
            if init is None:
                del cells[name]["parameters"]["INIT"]

        def lut(name, q, bits, init):
            cells[name] = {"type": "LUT"+str(len(bits)), "parameters": {"INIT":init}, "attributes": {},
                           "port_directions": {**{f"I{i}":"input" for i in range(len(bits))}, "O":"output"},
                           "connections": {**{f"I{i}":[bit] for i,bit in enumerate(bits)}, "O":[q]}}

        register("kept", 4, 3)
        register("dead", 5, 6)
        lut("dead_feedback", 6, [5], "10")
        register("zero", 7, "0", init="x", ce=3)
        register("zero_chain", 8, 7)
        register("dynamic", 9, 3)
        register("initial_transition", 10, "0", init="1")
        register("one", 11, "1", init="1", kind="FDSE")
        register("reset_changes_one", 12, "1", init="1", reset=3)
        register("disabled_zero", 13, 3, ce="0", reset=3)
        register("not_constant_chain", 14, 9)
        register("mutual_a", 15, 16)
        register("mutual_b", 16, 15, init="x")
        register("and_feedback", 17, 18)
        lut("and_lut", 18, [17,3], "1000")
        register("inverted_data", 19, "1", parameters={"IS_D_INVERTED":"1"})
        register("inverted_reset", 20, 3, parameters={"IS_R_INVERTED":"1"})
        register("unknown_lut", 21, 22)
        lut("unknown_lut_driver", 22, [21], "xx")
        register("disabled_unknown_init", 23, 3, init="x", ce="0")
        register("mutual_known_a", 24, 25)
        register("mutual_known_b", 25, 24)
        register("implicit_set_init", 26, "0", kind="FDSE", init=None)
        register("implicit_async_set_init", 27, "1", kind="FDPE", init=None)
        outputs = [4] + list(range(7,28))
        source = root / "source.json"
        source.write_text(json.dumps({"modules":{"dut":{"attributes":{"top":"1"}, "cells":cells,
            "ports":{"clk":{"direction":"input","bits":[2]}, "d":{"direction":"input","bits":[3]},
                     "q":{"direction":"output","bits":outputs}},
            "netnames":{f"n{i}":{"bits":[i]} for i in range(2,28)}}}}))
        ir = root / "source.emuir.sqlite"
        import_yosys_json(source).save(ir)
        report = emit_vivado_mapped_verilog(ir, root / "dut.v")
        mapped, functions = Path(report["mapped_index"]["path"]), Path(report["source_functions"]["path"])
        with closing(sqlite3.connect(mapped)) as db, db:
            origins = dict(db.execute("SELECT id,seq FROM cells"))
        inventory = root / "inventory.tsv"
        with inventory.open("w") as stream:
            writer = csv.writer(stream, delimiter="\t",lineterminator="\n")
            writer.writerow(INVENTORY_HEADER)
            if not empty:
                writer.writerow(["partition/kept", "FDRE", f"s{origins['kept']}", "", "SLICE_X0Y0", "AFF", "0", "0", "LEAF"])
        lineage = root / "lineage.sqlite"
        import_cell_lineage(mapped, functions, {stage:inventory}, lineage,
                            mapped_sha256=report["mapped_index"]["sha256"], retain_stage=stage)
        return mapped, lineage, report["mapped_index"]["sha256"], origins

    def proofs(self, root):
        mapped, lineage, digest, origins = self.prepare(root)
        output = root / "proofs.sqlite"
        read_text = Path.read_text
        def control_document_only(path, *args, **kwargs):
            if path in (mapped, lineage, output) or path.stat().st_size > 256 * 1024:
                raise AssertionError("whole-document read")
            return read_text(path, *args, **kwargs)
        with patch.object(Path, "read_text", control_document_only):
            report = build_optimization_proofs(mapped, lineage, output, mapped_sha256=digest)
        with closing(sqlite3.connect(output)) as db, db:
            rows = {name:db.execute("SELECT reason,constant_value,initialization FROM eliminated_sources WHERE origin=?", (origin,)).fetchone()
                    for name,origin in origins.items()}
            report["conditional_candidates"] = {name: db.execute("SELECT constant_value,initialization FROM constant_candidates WHERE origin=?", (origin,)).fetchone()
                                                 for name,origin in origins.items()}
            self.assertEqual([row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
                             ["constant_candidates", "eliminated_sources", "metadata", "source_names"])
        return report, rows

    def test_unobservable_cycles_and_observed_frontier(self):
        with tempfile.TemporaryDirectory() as directory:
            report, rows = self.proofs(Path(directory))
            self.assertEqual(report["unobservable_source_cells"], 2)
            self.assertEqual(rows["dead"], ("unobservable-source-closure",None,None))
            self.assertEqual(rows["dead_feedback"], rows["dead"])
            self.assertIsNone(rows["dynamic"])
            self.assertIsNone(rows["kept"])

    def test_joint_constant_proof_and_initialization_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            report, rows = self.proofs(Path(directory))
            for name in ["zero", "zero_chain", "mutual_a", "mutual_b", "disabled_unknown_init"]:
                self.assertIsNone(rows[name], name)
                self.assertIsNotNone(report["conditional_candidates"][name], name)
            self.assertEqual(report["conditional_candidates"]["zero"], (0,"vendor-dont-care-selection"))
            self.assertEqual(report["conditional_candidates"]["zero_chain"], (0,"specified"))
            for name in ["mutual_known_a", "mutual_known_b"]:
                self.assertEqual(rows[name], ("constant-state",0,"specified"))
            self.assertEqual(rows["one"], ("constant-state",1,"specified"))
            self.assertIsNone(rows["implicit_set_init"])
            self.assertEqual(rows["implicit_async_set_init"], ("constant-state",1,"specified"))
            self.assertIn("candidates-are-not-proofs", report["qualification"])

    def test_nonconstant_initial_reset_and_rejected_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            report, rows = self.proofs(Path(directory))
            for name in ["dynamic", "initial_transition", "reset_changes_one", "not_constant_chain", "unknown_lut"]:
                self.assertIsNone(rows[name], name)
            self.assertGreaterEqual(report["constant_refinement_iterations"], 2)

    def test_lut_cofactor_control_and_inversions(self):
        with tempfile.TemporaryDirectory() as directory:
            _, rows = self.proofs(Path(directory))
            for name in ["and_feedback", "disabled_zero", "inverted_data", "inverted_reset"]:
                self.assertEqual(rows[name], ("constant-state",0,"specified"), name)

    def test_stage_source_binding_empty_census_and_failed_output_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapped,lineage,digest,_ = self.prepare(root, stage="synthesized", empty=True)
            output = root / "proofs.sqlite"
            with self.assertRaisesRegex(ValidationError, "different source or stage"):
                build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest)
            self.assertFalse(output.exists())
            with self.assertRaisesRegex(ValidationError, "different source or stage"):
                build_optimization_proofs(mapped,lineage,output,mapped_sha256="0"*64,stage="synthesized")
            self.assertFalse(output.exists())
            report = build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest,stage="synthesized")
            self.assertEqual(report["stage"], "synthesized")
            with self.assertRaisesRegex(ValidationError, "already exists"):
                build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest,stage="synthesized")

    def test_proof_reader_checks_partition_source_and_conditional_initialization(self):
        from emuflow.timing_disposition import require_optimization_proof
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapped,lineage,digest,origins = self.prepare(root)
            output = root / "proofs.sqlite"
            build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest,fpga="fpga0")
            with self.assertRaisesRegex(ValidationError, "partition or stage"):
                OptimizationProofView(output, expected_fpga="other")
            with self.assertRaisesRegex(ValidationError, "digest disagrees"):
                OptimizationProofView(output, expected_fpga="fpga0", expected_sha256="0"*64)
            with OptimizationProofView(output, expected_fpga="fpga0") as proofs:
                source = f"s{origins['dead']}"
                physical = {"optimization_proofs":{"fpga0":proofs}}
                record = {"start_pin":"kept/Q", "end_pin":"dead/D"}
                self.assertEqual(require_optimization_proof(physical,"fpga0",source,record)["source_cell"],"dead")
                with self.assertRaisesRegex(ValidationError,"different source endpoint"):
                    require_optimization_proof(physical,"fpga0",source,{"start_pin":"kept/Q","end_pin":"dynamic/D"})
                with self.assertRaisesRegex(ValidationError,"unproved or conditional"):
                    proofs.require_eliminated_source(f"s{origins['zero']}")
                with self.assertRaisesRegex(ValidationError,"proof owner"):
                    require_optimization_proof({},"fpga0",source,record)
            with self.assertRaisesRegex(ValidationError,"closed"):
                proofs.require_eliminated_source(source)

    def test_proof_publication_is_relocatable_lazy_and_bound_to_the_input(self):
        from emuflow.physical_summary_storage import publish_optimization_proofs, read_physical_summary, canonical_physical_summary_sha256
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapped,lineage,digest,origins = self.prepare(root)
            output = root/"proofs.sqlite"
            build_optimization_proofs(mapped,lineage,output,mapped_sha256=digest,fpga="fpga0")
            published = root/"published"
            with self.assertRaisesRegex(ValidationError,"different physical input"):
                publish_optimization_proofs(output,published,Path("fpga0/proofs.sqlite"),fpga="fpga0",mapped_sha256="0"*64)
            reference = publish_optimization_proofs(output,published,Path("fpga0/proofs.sqlite"),fpga="fpga0",mapped_sha256=digest)
            summary = published/"summary.json"
            summary.write_text(json.dumps({"optimization_proofs":{"fpga0":reference}}))
            with read_physical_summary(summary) as physical:
                view = physical["optimization_proofs"]["fpga0"]
                self.assertIs(view,physical["optimization_proofs"]["fpga0"])
                self.assertEqual(view.require_eliminated_source(f"s{origins['dead']}")["source_cell"],"dead")
                self.assertEqual(len(canonical_physical_summary_sha256(physical)),64)
            with self.assertRaisesRegex(ValidationError,"closed"):
                view.require_eliminated_source(f"s{origins['dead']}")

    @unittest.skipUnless(shutil.which("tclsh"), "requires Tcl interpreter")
    def test_eliminated_local_query_exports_a_disposition_and_imports_without_a_delay(self):
        from emuflow.vivado_physical_timing import import_vivado_local_timing
        from emuflow.local_path_timing_storage import read_local_path_timing_storage
        from tests.test_local_path_timing_storage import _header, _record
        from emuflow.local_path_timing import path_id_set_sha256
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapped,lineage,digest,origins = self.prepare(root)
            proofs = root / "proofs.sqlite"
            build_optimization_proofs(mapped,lineage,proofs,mapped_sha256=digest,fpga="fpga0")
            query,resolved,measured = root/"query.tsv",root/"resolved.tsv",root/"measurements.tsv"
            query.write_text("endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex\n"+
                "\t".join(["p".encode().hex(),"local","pin","kept/Q".encode().hex(),"pin","dead/D".encode().hex(),"",""])+"\n")
            functions = mapped.with_name("dut.functions.sqlite")
            resolve_timing_query(mapped,functions,lineage,query,resolved,optimization_proofs=proofs,allow_eliminated=True)
            self.assertIn(f"optimized-away:s{origins['dead']}",resolved.read_text())
            mock = r'''
if {[catch {binary decode hex 61}]} {
  rename binary native_binary
  proc binary {operation format value} {
    if {$operation eq "decode"} {return [native_binary format H* $value]}
    native_binary scan $value H* result; return $result
  }
}
proc open_checkpoint {args} {}
proc get_clocks {args} {return {}}
proc get_pins {args} {error "eliminated path was resolved as a physical pin"}
proc get_timing_paths {args} {error "eliminated path was numerically measured"}
'''
            helper = Path(__file__).resolve().parents[1]/"scripts/vivado/export_logic_segment_timing.tcl"
            wrapper = root/"run.tcl"
            wrapper.write_text(mock+f"\nset argv [list fake.dcp {{{resolved}}} {{{measured}}} {{}}]\nset argc 4\nsource {{{helper}}}\n")
            result = subprocess.run([shutil.which("tclsh"),str(wrapper)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stderr,"")
            header = _header(); header["coverage"]["local_paths"] = 1
            header["source"].update(original_paths=1,original_path_ids_sha256=path_id_set_sha256(["p"]))
            record = {**_record("p",1.0),"start_pin":"kept/Q","end_pin":"dead/D",
                      "measurement":"endpoint-longest-path-fallback","path_pins":[]}
            del record["delay_ns"]
            identity = root/"identity.sqlite"
            with closing(sqlite3.connect(identity)) as db, db:
                db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
                db.execute("CREATE TABLE paths(id TEXT PRIMARY KEY,record_json TEXT)")
                db.execute("INSERT INTO metadata VALUES ('header',?)",(json.dumps({**header,"schema":"emuflow.local-path-identity/v2"}),))
                db.execute("INSERT INTO paths VALUES ('p',?)",(json.dumps(record),))
            with self.assertRaisesRegex(ValidationError,"proof owner"):
                import_vivado_local_timing(measured,identity,root/"rejected.json")
            with OptimizationProofView(proofs,expected_fpga="fpga0") as view:
                report = import_vivado_local_timing(measured,identity,root/"timing.json",optimization_proofs=view)
            self.assertEqual(report["optimized_away_paths"],1)
            with read_local_path_timing_storage(root/"timing.json") as view:
                self.assertIsNone(view["paths"][0]["delay_ns"])
                self.assertEqual(view["paths"][0]["optimization_source"],f"s{origins['dead']}")
            query.write_text(query.read_text().replace("dead/D".encode().hex(),"zero/D".encode().hex()))
            with self.assertRaisesRegex(ValidationError,"no proven endpoint"):
                resolve_timing_query(mapped,functions,lineage,query,root/"conditional.tsv",optimization_proofs=proofs,allow_eliminated=True)

    def test_indexed_logic_import_conserves_eliminated_and_measured_members(self):
        from emuflow.logic_segment_identity_storage import write_logic_segment_identity_storage
        from emuflow.logic_segment_timing import import_vivado_logic_segment_timing, VIVADO_LOGIC_SEGMENT_TIMING_HEADER
        from emuflow.logic_segment_timing_storage import read_logic_segment_timing_storage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapped,lineage,digest,origins = self.prepare(root)
            proofs = root/"proofs.sqlite"
            build_optimization_proofs(mapped,lineage,proofs,mapped_sha256=digest,fpga="fpga0")
            identity = root/"identity.json"
            header = {"schema":"emuflow.logic-segment-identity/v1","status":"pass","design":"dut",
                      "platform":"board","fpga":"fpga0","provider":"test","coverage":{},
                      "system_routes_sha256":"a"*64,"tdm_schedule_sha256":"b"*64}
            records = [{"id":f"l{i}","kind":"launch","system_path":f"p{i}","member_path":f"m{i}",
                        "cut_index":0,"fpga":"fpga0","replace_tx_endpoint":f"tx{i}",
                        "start_pin":"kept/Q","end_pin":"dead/D" if i == 0 else "kept/D"} for i in range(2)]
            with write_logic_segment_identity_storage(identity,header=header,segments=records): pass
            raw = root/"timing.tsv"
            rows = [[r["id"].encode().hex(),r["kind"],"" if i == 0 else "2.5",
                     r["start_pin"].encode().hex(),r["end_pin"].encode().hex(),
                     f"optimized-away:s{origins['dead']}" if i == 0 else "endpoint-exact",
                     "" if i == 0 else "physical/Q".encode().hex(),"" if i == 0 else "physical/D".encode().hex()]
                    for i,r in enumerate(records)]
            def write(): raw.write_text(VIVADO_LOGIC_SEGMENT_TIMING_HEADER+"\n"+"".join("\t".join(row)+"\n" for row in rows))
            write()
            with self.assertRaisesRegex(ValidationError,"proof owner"):
                import_vivado_logic_segment_timing(raw,identity,root/"rejected.json")
            with OptimizationProofView(proofs,expected_fpga="fpga0") as proof:
                result = import_vivado_logic_segment_timing(raw,identity,root/"timing.json",optimization_proofs=proof)
                self.assertEqual(result["optimized_away_segments"],1)
                self.assertEqual(result["endpoint_exact_segments"],1)
                with read_logic_segment_timing_storage(root/"timing.json") as view:
                    self.assertEqual(view.selector["user_version"],3)
                    self.assertEqual(len(view["segments"]),2)
                    self.assertIsNone(view["segments"][0]["delay_ns"])
                    self.assertNotIn("actual_end_object",view["segments"][0])
                    self.assertEqual(view["segments"][1]["delay_ns"],2.5)
                    # Removing the nullable SQL guard is rejected even with a
                    # refreshed byte seal; it is part of the typed contract.
                    import emuflow.logic_segment_timing_storage as store
                    with closing(sqlite3.connect(view.store_path)) as db, db:
                        with self.assertRaises(sqlite3.IntegrityError):
                            db.execute("UPDATE segments SET delay_ns=0 WHERE measurement='optimized-away'")
                        with self.assertRaises(sqlite3.IntegrityError):
                            db.execute("UPDATE segments SET sampling_cost_ns=0 WHERE measurement='optimized-away'")
                from emuflow.logic_segment_timing import VIVADO_LOGIC_SAMPLING_COST_HEADER
                cost_raw = root/"sampling.tsv"
                cost_rows = [row + (["", ""] if i == 0 else ["1.25", "-1.25"])
                             for i, row in enumerate(rows)]
                cost_raw.write_text(VIVADO_LOGIC_SAMPLING_COST_HEADER+"\n"+
                                    "".join("\t".join(row)+"\n" for row in cost_rows))
                result = import_vivado_logic_segment_timing(
                    cost_raw, identity, root/"sampling.json", optimization_proofs=proof)
                self.assertEqual(result["optimized_away_segments"], 1)
                with read_logic_segment_timing_storage(root/"sampling.json") as view:
                    self.assertNotIn("sampling_cost_ns", view["segments"][0])
                    self.assertEqual(view["segments"][1]["sampling_cost_ns"], 1.25)
                rows[0][2] = "0"; write()
                with self.assertRaises(ValidationError):
                    import_vivado_logic_segment_timing(raw,identity,root/"fake.json",optimization_proofs=proof)
                rows[0][2] = ""; rows[0][5] = f"optimized-away:s{origins['one']}"; write()
                with self.assertRaisesRegex(ValidationError,"different source endpoint"):
                    import_vivado_logic_segment_timing(raw,identity,root/"borrowed.json",optimization_proofs=proof)

    @unittest.skipUnless(shutil.which("tclsh"), "requires Tcl interpreter")
    def test_boundary_elimination_tcl_import_and_global_owner_validation(self):
        from emuflow.boundary_identity_storage import write_boundary_identity_storage
        from emuflow.boundary_timing_storage import read_boundary_timing_storage
        from emuflow.vivado_physical_timing import import_indexed_vivado_boundary
        from emuflow.physical_timing_inputs import _endpoint_delay_database
        from tests.test_boundary_identity_storage import _header, _record
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mapped,lineage,digest,origins = self.prepare(root)
            proofs = root/"proof.sqlite"
            build_optimization_proofs(mapped,lineage,proofs,mapped_sha256=digest,fpga="fpga0")
            identity = _record("rx-0","rx",0)
            identity["merged_ir"]["boundary_register_instances"] = ["dead"]
            path = root/"identity.json"
            with write_boundary_identity_storage(path,_header(),[identity]): pass
            query,resolved,measured = root/"query.tsv",root/"resolved.tsv",root/"timing.tsv"
            query.write_text("endpoint_hex\tkind\texternal_port_hex\tbit\tlogical_net_hex\tboundary_cell_hex\n"+
                             "\t".join(["rx-0".encode().hex(),"rx","input".encode().hex(),"0","n".encode().hex(),"dead".encode().hex()])+"\n")
            resolve_timing_query(mapped,mapped.with_name("dut.functions.sqlite"),lineage,query,resolved,
                                 boundary=True,optimization_proofs=proofs,allow_eliminated=True)
            wrapper = root/"run.tcl"
            helper = Path(__file__).resolve().parents[1]/"scripts/vivado/export_boundary_timing.tcl"
            mock = '''
if {[catch {binary decode hex 61}]} {
 rename binary native_binary
 proc binary {operation format value} {
  if {$operation eq "decode"} {return [native_binary format H* $value]}
  native_binary scan $value H* result; return $result
 }
}
proc open_checkpoint {args} {}
proc get_clocks {args} {return {}}
proc get_ports {args} {error "removed interface must not invent a physical port"}
proc get_timing_paths {args} {error "removed interface must not have a numerical delay"}
'''
            wrapper.write_text(mock+f"\nset argv [list fake.dcp {{{resolved}}} {{{measured}}}]\nset argc 3\nsource {{{helper}}}\n")
            result = subprocess.run([shutil.which("tclsh"),str(wrapper)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            with self.assertRaisesRegex(ValidationError,"proof owner"):
                import_indexed_vivado_boundary(measured,path,root/"no-owner.json")
            with OptimizationProofView(proofs,expected_fpga="fpga0") as proof:
                report = import_indexed_vivado_boundary(measured,path,root/"timing.json",optimization_proofs=proof)
                self.assertIsNone(report["maximum_delay_ns"])
                self.assertEqual(report["optimized_away_endpoints"],1)
                cost_measured = root/"costs.tsv"
                wrapper.write_text(mock+f"\nset argv [list fake.dcp {{{resolved}}} {{{cost_measured}}} {{}} {{}} clock-aware-reference]\nset argc 6\nsource {{{helper}}}\n")
                exported = subprocess.run([shutil.which("tclsh"),str(wrapper)],capture_output=True,text=True)
                self.assertEqual(exported.returncode,0,exported.stderr)
                assert len(cost_measured.read_text().splitlines()[1].split("\t")) == 8
                import_indexed_vivado_boundary(cost_measured,path,root/"cost-timing.json",optimization_proofs=proof)
                with read_boundary_timing_storage(root/"cost-timing.json") as cost_timing:
                    self.assertNotIn("sampling_cost_ns",cost_timing["endpoints"][0])
                    self.assertEqual(cost_timing["sampling_cost_policy"],"vivado-clock-aware-reference-v1")
                from emuflow.boundary_identity_storage import read_boundary_identity_storage
                with read_boundary_timing_storage(root/"timing.json") as timing, read_boundary_identity_storage(path) as identities:
                    physical = {"boundary_timing":{"fpga0":timing},"boundary_identities":{"fpga0":identities},
                                "optimization_proofs":{"fpga0":proof}}
                    endpoints = _endpoint_delay_database(physical)
                    self.assertIsNone(endpoints["rx-0"]["delay_ns"])
                    self.assertIsNone(endpoints["rx-0"]["start_object"])
                    identity["merged_ir"]["boundary_register_instances"] = ["one"]
                    physical["boundary_identities"] = {"fpga0":{**_header(),"endpoints":[identity]}}
                    with self.assertRaisesRegex(ValidationError,"different source endpoint"):
                        _endpoint_delay_database(physical)
