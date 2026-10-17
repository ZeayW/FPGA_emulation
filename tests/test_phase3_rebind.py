import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.phase3 import run_phase3, validate_phase3_indexed
from emuflow.phase3_rebind import rebind_phase3_assignment
from tests.test_combinational_cut import _chain_ir, PLATFORM_PATH


class Phase3RebindTest(unittest.TestCase):
    def prepare(self, root):
        ir = _chain_ir()
        source = root / "old.sqlite"
        write_compact_emuir(source, ir.to_dict())
        constraints = root / "constraints.json"
        write_json(constraints, {
            "schema": "emuflow.partition-constraints/v1",
            "balance_tolerance": 2.1,
            "fixed": [{"instance": key, "fpga": target} for key, target in
                      [("q0", "fpga0"), ("l0", "fpga0"), ("l1", "fpga1"),
                       ("l2", "fpga1"), ("q1", "fpga1")]],
        })
        frozen = root / "frozen"
        run_phase3(source, PLATFORM_PATH, frozen, constraints_path=constraints,
                   provider="greedy", seed=7, cut_mode="static-exact-combinational")
        return ir, frozen

    def owners(self, directory):
        with sqlite3.connect(directory / "assignment.sqlite") as c:
            return c.execute("SELECT c.copy_id,p.fpga FROM copies c JOIN parts p "
                             "ON p.part_index=c.part_index ORDER BY c.copy_id").fetchall()

    def test_rebuilds_changed_connectivity_without_partitioning_or_circuit_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir, frozen = self.prepare(root)
            old = read_json(frozen / "assignment.json")
            value = copy.deepcopy(ir.to_dict())
            next(n for n in value["nets"] if n["id"] == "d")["sinks"] = []
            repaired = root / "repaired.sqlite"
            write_compact_emuir(repaired, EmuIR(value).to_dict())
            output = root / "rebound"
            with patch("emuflow.phase3.run_tritonpart", side_effect=AssertionError("no optimizer")), \
                 patch("emuflow.partition_ir.EmuIR.load", side_effect=AssertionError("no whole circuit")), \
                 patch("emuflow.phase3_storage.expand_phase3_assignment", side_effect=AssertionError("no old graph")):
                report = rebind_phase3_assignment(repaired, PLATFORM_PATH, frozen, output)
            new = read_json(output / "assignment.json")
            self.assertEqual(self.owners(frozen), self.owners(output))
            self.assertLess(new["semantic_contract"]["metrics"]["capture_requirements"],
                            old["semantic_contract"]["metrics"]["capture_requirements"])
            self.assertFalse(report["partition_decision"]["executed"])
            self.assertEqual(report["partition_decision"]["preserved_instance_owners"], 5)
            self.assertEqual(json.loads((output / "constraints.normalized.json").read_text())
                             ["balance_tolerance"], 2.1)
            self.assertEqual(validate_phase3_indexed(repaired, PLATFORM_PATH,
                             output / "phase3_report.json", output / "assignment.sqlite"),
                             report["validation"])
            with self.assertRaisesRegex(ValidationError, "new output"):
                rebind_phase3_assignment(repaired, PLATFORM_PATH, frozen, output)

    def test_rejects_same_count_wrong_instance_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir, frozen = self.prepare(root)
            value = ir.to_dict()
            value["instances"][0]["id"] = "replacement"
            for net in value["nets"]:
                for endpoint in net["drivers"] + net["sinks"]:
                    if endpoint["instance"] == "q0":
                        endpoint["instance"] = "replacement"
            changed = root / "changed.sqlite"
            write_compact_emuir(changed, EmuIR(value).to_dict())
            with self.assertRaises(ValidationError):
                rebind_phase3_assignment(changed, PLATFORM_PATH, frozen, root / "out")
            self.assertFalse((root / "out/phase3_report.json").exists())

    def test_rejects_capacity_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir, frozen = self.prepare(root)
            value = ir.to_dict()
            next(i for i in value["instances"] if i["id"] == "l0")["resources"]["lut"] = 10000000
            changed = root / "changed.sqlite"
            write_compact_emuir(changed, EmuIR(value).to_dict())
            with self.assertRaisesRegex(ValidationError, "capacity|cannot fit"):
                rebind_phase3_assignment(changed, PLATFORM_PATH, frozen, root / "out")

    def test_rejects_frozen_ownership_that_splits_current_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir, frozen = self.prepare(root)
            constraints = json.loads((frozen / "constraints.normalized.json").read_text())
            constraints["fixed"] = []
            constraints["groups"] = [{"id": "new-atomic-group", "instances": ["l0", "l1"]}]
            write_json(frozen / "constraints.normalized.json", constraints)
            with self.assertRaisesRegex(ValidationError, "splits a current atomic cluster"):
                rebind_phase3_assignment(root / "old.sqlite", PLATFORM_PATH, frozen, root / "out")


if __name__ == "__main__":
    unittest.main()
