from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.cut_segment_qualification import (
    CUT_SEGMENT_QUALIFICATION_SCHEMA,
    build_cut_segment_qualification,
    validate_cut_segment_qualification,
)
from emuflow.io import write_json
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
        "emuflow.cut_segment_qualification.validate_sta_path_database_value",
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
        "emuflow.cut_segment_qualification.validate_sta_path_database_value",
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
