from __future__ import annotations

import tempfile
import unittest
import json
import sqlite3
from pathlib import Path
from unittest import mock

from emuflow.cut_segment_qualification import (
    CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA,
    CUT_SEGMENT_QUALIFICATION_SCHEMA,
    build_cut_segment_qualification,
    build_cut_segment_qualification_value,
    validate_cut_segment_qualification,
)
from tests.phase3_index_fixture import write_assignment_v3_fixture
from emuflow.io import write_json
from emuflow.ir import EmuIR
from emuflow.opensta import (
    DEFAULT_TIMING_MODEL,
    classify_through_net_timing_endpoints,
    load_timing_model,
)
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


class CutSegmentQualificationTest(unittest.TestCase):
    def _fixture(self, root: Path):
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json",
            top="counter",
            clocks=["clk"],
        )
        model = load_timing_model(DEFAULT_TIMING_MODEL)
        all_nets = [net["id"] for net in ir.value["nets"]]
        structural = classify_through_net_timing_endpoints(
            ir, model, all_nets
        )
        cut_net = next(
            net for net in all_nets if structural[net]["status"] == "timed"
        )
        ir_path = root / "design.emuir.json"
        assignment_path = root / "assignment.json"
        database_path = root / "path-database.json"
        write_json(ir_path, ir.value)
        write_json(
            assignment_path,
            {
                "schema": "emuflow.partition-assignment/v1",
                "cut_nets": [{"net": cut_net}],
                "semantic_contract": {
                    "cut_nodes": [
                        {
                            "net": cut_net,
                            "source_segment_ids": ["segment000000"],
                            "capture_segment_ids": ["segment000001"],
                        }
                    ]
                },
            },
        )
        write_json(
            database_path,
            {
                "schema": "emuflow.sta-path-database/v1",
                "paths": [
                    {
                        "id": "path0",
                        "path_nets": [cut_net],
                    }
                ],
            },
        )
        return ir_path, assignment_path, database_path, cut_net

    @mock.patch(
        "emuflow.cut_segment_qualification.validate_sta_path_database_path_value",
        return_value={"status": "pass", "paths": 1},
    )
    def test_reconstructs_structural_cut_and_segment_identity(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            artifact = build_cut_segment_qualification(*paths[:3])
        self.assertEqual(artifact["schema"], CUT_SEGMENT_QUALIFICATION_SCHEMA)
        self.assertEqual(artifact["summary"]["timed_structural_nets"], 1)
        record = artifact["records"][0]
        self.assertEqual(record["net"], paths[3])
        self.assertEqual(record["associated_original_path_ids"], ["path0"])
        self.assertEqual(record["source_segment_ids"], ["segment000000"])
        self.assertEqual(record["capture_segment_ids"], ["segment000001"])

    @mock.patch(
        "emuflow.cut_segment_qualification.validate_sta_path_database_path_value",
        return_value={"status": "pass", "paths": 1},
    )
    def test_independent_rebuild_rejects_tampering(self, _validate):
        with tempfile.TemporaryDirectory() as temporary:
            ir_path, assignment_path, database_path, _ = self._fixture(
                Path(temporary)
            )
            artifact = build_cut_segment_qualification(
                ir_path, assignment_path, database_path
            )
            checked = validate_cut_segment_qualification(
                artifact, ir_path, assignment_path, database_path
            )
            self.assertEqual(checked["status"], "pass")
            artifact["records"][0]["source_segment_ids"] = ["forged"]
            with self.assertRaisesRegex(Exception, "independently reconstructed"):
                validate_cut_segment_qualification(
                    artifact, ir_path, assignment_path, database_path
                )

    def test_single_fpga_zero_cut_qualification_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            ir = import_yosys_json(
                ROOT / "examples/yosys/counter.json",
                top="counter",
                clocks=["clk"],
            )
            artifact = build_cut_segment_qualification_value(
                ir,
                {
                    "cut_nets": [],
                    "semantic_contract": {"cut_nodes": []},
                },
                {"paths": []},
            )
        self.assertEqual(artifact["status"], "pass")
        self.assertEqual(artifact["cut_nets"], [])
        self.assertEqual(
            artifact["summary"],
            {
                "cut_nets": 0,
                "timed_structural_nets": 0,
                "no_timed_endpoint_nets": 0,
                "enumerated_member_associations": 0,
                "functional_only_nets": 0,
            },
        )

    def test_compact_classification_matches_legacy_without_graph_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path, assignment_path, database_path, _ = self._fixture(root)
            compact_path = root / "design.emuir.sqlite"
            EmuIR.load(ir_path).save(compact_path)
            legacy = build_cut_segment_qualification(
                ir_path,
                assignment_path,
                database_path,
                input_already_validated=True,
            )
            with mock.patch(
                "emuflow.cut_segment_qualification.EmuIR.load",
                side_effect=AssertionError("whole-graph loading forbidden"),
            ):
                compact = build_cut_segment_qualification(
                    compact_path,
                    assignment_path,
                    database_path,
                    input_already_validated=True,
                )
            self.assertEqual(compact, legacy)

    def test_indexed_qualification_persists_bounded_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir_path, assignment_path, database_path, cut_net = self._fixture(root)
            compact_path = root / "design.emuir.sqlite"
            EmuIR.load(ir_path).save(compact_path)
            write_assignment_v3_fixture(
                root / "assignment.sqlite", parts=("fpga0", "fpga1"),
                metadata={
                    "design": "counter", "platform": "two", "provider": "test",
                    "seed": 0, "constraints": {}, "semantic_contract_sha256": "b" * 64,
                },
                cut_sinks=[(cut_net, 0, 1)],
                semantic={
                    "schema": "emuflow.static-exact-combinational-cut/v3",
                    "cut_nodes": [{"net": cut_net, "source_segment_ids": ["segment000000"]}],
                    "logic_segments": [
                        {"id": "segment000000", "kind": "launch_to_tx", "fpga": "fpga0", "sink_cut_net": cut_net},
                        {"id": "segment000001", "kind": "rx_to_capture", "fpga": "fpga1", "source_cut_net": cut_net, "capture_requirement": "capture000000"},
                    ],
                    "capture_requirements": [
                        {"id": "capture000000", "cut_net": cut_net, "fpga": "fpga1", "kind": "top-output", "endpoint": "out"},
                    ],
                },
            )
            artifact = build_cut_segment_qualification(
                compact_path,
                assignment_path,
                database_path,
                input_already_validated=True,
            )
            self.assertEqual(
                artifact["schema"], CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA
            )
            self.assertEqual(artifact["storage"], "indexed-summary")
            self.assertEqual(artifact["summary"]["cut_nets"], 1)
            self.assertEqual(
                artifact["relation_counts"],
                {
                    "cut_sinks": 1,
                    "exact_cut_nodes": 1,
                    "exact_cut_source_segments": 1,
                    "exact_consumer_segments": 1,
                },
            )
            self.assertNotIn("records", artifact)
