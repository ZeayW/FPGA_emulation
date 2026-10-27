from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from emuflow.combinational_cut import semantic_contract_sha256
from emuflow.cut_segment_qualification import (
    CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA,
    CUT_SEGMENT_QUALIFICATION_SCHEMA,
    build_cut_segment_qualification,
    build_cut_segment_qualification_value,
    validate_cut_segment_qualification,
)
from emuflow.emuir_compact import write_compact_emuir
from emuflow.io import write_json
from emuflow.opensta import (
    DEFAULT_TIMING_MODEL,
    classify_through_net_timing_endpoints,
    load_timing_model,
)
from emuflow.yosys import import_yosys_json
from tests.phase3_store_fixture import write_current_assignment_fixture


ROOT = Path(__file__).resolve().parents[1]


def sealed_rows(rows):
    """Independent encoding of the qualification's ordered identity facts."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


class CutSegmentQualificationTest(unittest.TestCase):
    def _fixture(self, root: Path):
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
        )
        instances = {item["id"]: item for item in ir.value["instances"]}
        structural = classify_through_net_timing_endpoints(
            ir, load_timing_model(DEFAULT_TIMING_MODEL),
            [net["id"] for net in ir.value["nets"]],
        )
        # One explicit LUT-to-register cut keeps this a small qualification
        # fact fixture; it does not implement partitioning or an old publisher.
        net = next(net for net in ir.value["nets"]
                   if structural[net["id"]]["status"] == "timed"
                   and len(net["drivers"]) == 1
                   and instances.get(net["drivers"][0].get("instance"), {}).get("type", "").startswith("LUT")
                   and any(instances.get(sink.get("instance"), {}).get("type") == "FDRE" for sink in net["sinks"]))
        cut_net = net["id"]
        capture = next(sink for sink in net["sinks"] if instances.get(sink.get("instance"), {}).get("type") == "FDRE")
        cut = {"net": cut_net, "cut_class": "combinational",
               "source_fpgas": ["fpga0"], "sink_fpgas": ["fpga1"],
               "sink_endpoints": 1, "dependency_level": 0,
               "combinational_dependency_depth": 0, "predecessor_cut_nets": []}
        contract = {
            "schema": "emuflow.static-exact-combinational-cut/v3",
            "mode": "static-exact-combinational", "qualification": "structural-partition-legality",
            "candidate_selection_policy": "assignment-derived-acyclic-v2",
            "metrics": {"transported_cut_nets": 1, "logic_segments": 2, "capture_requirements": 1, "dependency_edges": 0},
            "cut_nodes": [{**{key: value for key, value in cut.items() if key != "sink_endpoints"},
                           "source_segment_ids": ["segment000000"],
                           "capture_segment_ids": ["segment000001"]}],
            "dependency_edges": [],
            "logic_segments": [
                {"id": "segment000000", "kind": "launch_to_tx", "fpga": "fpga0", "sink_cut_net": cut_net},
                {"id": "segment000001", "kind": "rx_to_capture", "fpga": "fpga1", "source_cut_net": cut_net,
                 "capture_requirement": "capture000000"},
            ],
            "capture_requirements": [{"id": "capture000000", "cut_net": cut_net,
                "fpga": "fpga1", "kind": "architectural-state", "endpoint": capture["instance"],
                "port": capture["port"], "bit": capture["bit"]}],
        }
        owners = {name: "fpga1" if name == capture["instance"] else "fpga0" for name in instances}
        assignment = {
            "schema": "emuflow.partition-assignment/v1", "design": "counter", "platform": "two",
            "provider": "qualification-facts", "seed": 0, "constraints": {},
            "instance_assignment": owners,
            "cluster_assignment": {f"c{index:06d}": owners[name] for index, name in enumerate(sorted(owners))},
            "partitions": [{"fpga": part, "resources": {}} for part in ("fpga0", "fpga1")],
            "cut_nets": [cut], "semantic_contract": contract,
            "semantic_contract_sha256": semantic_contract_sha256(contract),
        }
        ir_path = root / "design.emuir.sqlite"
        assignment_path = root / "assignment.json"
        database_path = root / "path-database.json"
        database = {"schema": "emuflow.sta-path-database/v1",
                    "paths": [{"id": "path0", "path_nets": [cut_net]}]}
        write_compact_emuir(ir_path, ir.value)
        write_current_assignment_fixture(assignment_path, ir, assignment)
        write_json(database_path, database)
        return (ir_path, assignment_path, database_path), ir, assignment, database, structural

    def test_reconstructs_structural_cut_and_segment_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            _paths, ir, assignment, database, _structural = self._fixture(Path(temporary))
            artifact = build_cut_segment_qualification_value(ir, assignment, database)
        self.assertEqual(artifact["schema"], CUT_SEGMENT_QUALIFICATION_SCHEMA)
        self.assertEqual(artifact["summary"]["timed_structural_nets"], 1)
        record = artifact["records"][0]
        self.assertEqual(record["net"], assignment["cut_nets"][0]["net"])
        self.assertEqual(record["associated_original_path_ids"], ["path0"])
        self.assertEqual(record["source_segment_ids"], ["segment000000"])
        self.assertEqual(record["capture_segment_ids"], ["segment000001"])

    @mock.patch("emuflow.cut_segment_qualification.validate_sta_path_database_path_value",
                return_value={"status": "pass", "paths": 1})
    def test_independent_rebuild_rejects_tampering(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            paths, _ir, _assignment, _database, _structural = self._fixture(Path(temporary))
            artifact = build_cut_segment_qualification(*paths)
            self.assertEqual(validate_cut_segment_qualification(artifact, *paths)["status"], "pass")
            for field in ("structural_identity", "semantic_contract_sha256", "relation_counts"):
                damaged = copy.deepcopy(artifact)
                if field == "structural_identity":
                    damaged[field]["sha256"] = "0" * 64
                elif field == "relation_counts":
                    damaged[field]["exact_cut_source_segments"] += 1
                else:
                    damaged[field] = "0" * 64
                with self.subTest(field=field), self.assertRaisesRegex(Exception, "independently reconstructed"):
                    validate_cut_segment_qualification(damaged, *paths)

    def test_single_fpga_zero_cut_qualification_is_explicit(self):
        ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
        artifact = build_cut_segment_qualification_value(
            ir, {"cut_nets": [], "semantic_contract": {"cut_nodes": []}}, {"paths": []})
        self.assertEqual(artifact["status"], "pass")
        self.assertEqual(artifact["cut_nets"], [])
        self.assertEqual(artifact["summary"], {"cut_nets": 0, "timed_structural_nets": 0,
            "no_timed_endpoint_nets": 0, "enumerated_member_associations": 0, "functional_only_nets": 0})

    def test_compact_classification_matches_independent_facts_without_graph_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths, ir, assignment, database, structural = self._fixture(Path(temporary))
            reference = build_cut_segment_qualification_value(ir, assignment, database)
            with mock.patch("emuflow.cut_segment_qualification.EmuIR.load",
                            side_effect=AssertionError("whole-graph loading forbidden")):
                compact = build_cut_segment_qualification(*paths, input_already_validated=True)
            self.assertEqual(compact["summary"], reference["summary"])
            cut_net = assignment["cut_nets"][0]["net"]
            self.assertEqual(compact["cut_net_identity"], sealed_rows([cut_net]))
            self.assertEqual(compact["structural_identity"], sealed_rows([(cut_net, structural[cut_net])]))
            self.assertEqual(compact["semantic_contract_sha256"], semantic_contract_sha256(assignment["semantic_contract"]))

    def test_indexed_qualification_persists_bounded_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths, _ir, assignment, _database, _structural = self._fixture(root)
            with sqlite3.connect(root / "assignment.sqlite") as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            artifact = build_cut_segment_qualification(*paths, input_already_validated=True)
            self.assertEqual(artifact["schema"], CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA)
            self.assertEqual(artifact["storage"], "indexed-summary")
            self.assertEqual(artifact["summary"]["cut_nets"], 1)
            self.assertEqual(artifact["summary"]["enumerated_member_associations"], 1)
            self.assertEqual(artifact["semantic_contract_sha256"], assignment["semantic_contract_sha256"])
            self.assertEqual(artifact["relation_counts"], {"cut_sinks": 1, "exact_cut_nodes": 1,
                "exact_cut_source_segments": 1, "exact_consumer_segments": 1})
            self.assertNotIn("records", artifact)
