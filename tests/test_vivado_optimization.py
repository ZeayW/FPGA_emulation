import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.vivado_lineage import INVENTORY_HEADER, import_cell_lineage
from emuflow.vivado_netlist import emit_vivado_mapped_verilog
from emuflow.vivado_optimization import build_optimization_proofs
from emuflow.yosys import import_yosys_json


class VivadoOptimizationTests(unittest.TestCase):
    def prepare(self, root, *, stage="routed", empty=False):
        cells = {}

        def register(name, q, d, *, init="0", ce="1", reset="0", kind="FDRE", parameters=None):
            control = {"FDRE": "R", "FDSE": "S", "FDCE": "CLR", "FDPE": "PRE"}[kind]
            cells[name] = {"type": kind, "parameters": {"INIT": init, **(parameters or {})},
                           "attributes": {}, "port_directions": {"C":"input", "CE":"input", "D":"input",control:"input", "Q":"output"},
                           "connections": {"C":[2], "CE":[ce], "D":[d], control:[reset], "Q":[q]}}

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
        outputs = [4] + list(range(7,26))
        source = root / "source.json"
        source.write_text(json.dumps({"modules":{"dut":{"attributes":{"top":"1"}, "cells":cells,
            "ports":{"clk":{"direction":"input","bits":[2]}, "d":{"direction":"input","bits":[3]},
                     "q":{"direction":"output","bits":outputs}},
            "netnames":{f"n{i}":{"bits":[i]} for i in range(2,26)}}}}))
        ir = root / "source.emuir.sqlite"
        import_yosys_json(source).save(ir)
        report = emit_vivado_mapped_verilog(ir, root / "dut.v")
        mapped, functions = Path(report["mapped_index"]["path"]), Path(report["source_functions"]["path"])
        with sqlite3.connect(mapped) as db:
            origins = dict(db.execute("SELECT id,seq FROM cells"))
        inventory = root / "inventory.tsv"
        with inventory.open("w") as stream:
            writer = csv.writer(stream, delimiter="\t",lineterminator="\n")
            writer.writerow(INVENTORY_HEADER)
            if not empty:
                writer.writerow(["partition/kept", "FDRE", f"s{origins['kept']}", ""])
        lineage = root / "lineage.sqlite"
        import_cell_lineage(mapped, functions, {stage:inventory}, lineage,
                            mapped_sha256=report["mapped_index"]["sha256"], retain_stage=stage)
        return mapped, lineage, report["mapped_index"]["sha256"], origins

    def proofs(self, root):
        mapped, lineage, digest, origins = self.prepare(root)
        output = root / "proofs.sqlite"
        with patch.object(Path, "read_text", side_effect=AssertionError("whole-document read")):
            report = build_optimization_proofs(mapped, lineage, output, mapped_sha256=digest)
        with sqlite3.connect(output) as db:
            rows = {name:db.execute("SELECT reason,constant_value,initialization FROM eliminated_sources WHERE origin=?", (origin,)).fetchone()
                    for name,origin in origins.items()}
            report["conditional_candidates"] = {name: db.execute("SELECT constant_value,initialization FROM constant_candidates WHERE origin=?", (origin,)).fetchone()
                                                 for name,origin in origins.items()}
            self.assertEqual([row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
                             ["constant_candidates", "eliminated_sources", "metadata"])
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
