import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.partition import build_clusters, build_partition_assignment, normalize_partition_constraints
from emuflow.phase3_assignment_index import write_phase3_assignment_index
from emuflow.phase3_storage import read_phase3_assignment_view
from emuflow.routing import semantic_contract_for_routes
from emuflow.tdm import build_tdm_schedule, validate_tdm_schedule


class IndexedExactTDMTest(unittest.TestCase):
    def _routes(self, root):
        from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
        fixture = StaticExactCombinationalCutPartitionTest()
        fixture.setUp()
        ir, platform = fixture.ir, fixture.platform
        constraints = normalize_partition_constraints(
            {"schema": "emuflow.partition-constraints/v1", "balance_tolerance": 1.0},
            ir, platform)
        clusters = build_clusters(ir, constraints, cut_mode="static-exact-combinational")
        targets = {"q0": "fpga0", "l0": "fpga1", "l1": "fpga0", "l2": "fpga1", "q1": "fpga0"}
        assignment = build_partition_assignment(
            ir, platform, clusters, constraints,
            {c["id"]: targets[c["instances"][0]] for c in clusters["clusters"]},
            provider="indexed-exact-test", seed=0)
        write_phase3_assignment_index(root / "assignment.sqlite", ir, assignment)
        with sqlite3.connect(root / "assignment.sqlite") as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT net_id FROM exact_cut_nodes")},
                             {"q", "n0", "n1", "d"})
        (root / "assignment.json").write_text(json.dumps(assignment))
        indexed = read_phase3_assignment_view(root / "assignment.json", include_semantic_contract=False)
        routes = fixture._exact_routes(assignment)
        routes.pop("semantic_contract")
        routes["semantic_contract"] = semantic_contract_for_routes(indexed, routes)
        self.assertTrue(routes["semantic_contract"]["indexed"])
        self.assertEqual({r["cut_class"] for r in routes["routes"]},
                         {"register_output", "combinational", "register_input"})
        return routes, platform

    def test_indexed_mixed_cuts_keep_root_and_dependent_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, platform = self._routes(Path(temporary))
            schedule = build_tdm_schedule(routes, platform)
            checked = validate_tdm_schedule(routes, platform, schedule)
            self.assertEqual(checked["status"], "pass")
            self.assertTrue(any(r["predecessor_cut_nets"] for r in routes["routes"]))
            entries = {entry["net"]: entry for entry in schedule["entries"]}
            self.assertGreaterEqual(entries["q"]["slot"], 1)
            for predecessor, successor in (("q", "n0"), ("n0", "n1"), ("n1", "d")):
                self.assertGreaterEqual(entries[successor]["slot"],
                                        entries[predecessor]["arrival_slot"] + 1)
            # An early launch must still fail the independent readiness gate.
            altered = copy.deepcopy(schedule)
            altered["entries"][0]["slot"] = 0
            altered["entries"][0]["ready_slot"] = 0
            altered["entries"][0]["arrival_slot"] -= 1
            with self.assertRaisesRegex(ValidationError, "ready-slot mismatch"):
                validate_tdm_schedule(routes, platform, altered)

    def test_indexed_invalid_cut_class_remains_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, platform = self._routes(Path(temporary))
            routes["routes"][0]["cut_class"] = "primary_input"
            with self.assertRaisesRegex(ValidationError, "dependency metadata"):
                build_tdm_schedule(routes, platform)

    def test_indexed_unknown_predecessor_remains_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, platform = self._routes(Path(temporary))
            routes["routes"][-1]["predecessor_cut_nets"] = ["missing-cut"]
            with self.assertRaisesRegex(ValidationError, "unknown predecessors"):
                build_tdm_schedule(routes, platform)


if __name__ == "__main__":
    unittest.main()
