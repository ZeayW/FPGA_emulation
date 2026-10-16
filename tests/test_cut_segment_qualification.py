from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.cut_segment_qualification import (
    CUT_SEGMENT_QUALIFICATION_SCHEMA,
    build_cut_segment_qualification,
    build_cut_segment_qualification_value,
    validate_cut_segment_qualification,
)
from emuflow.io import read_json, write_json
from emuflow.ir import EmuIR
from emuflow.opensta import (
    DEFAULT_TIMING_MODEL,
    classify_through_net_timing_endpoints,
    load_timing_model,
)
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]


class CutSegmentQualificationTest(unittest.TestCase):
    def test_streamed_paths_do_not_rebuild_cut_index_per_member(self):
        class CountedCuts(list):
            def __init__(self, values):
                super().__init__(values)
                self.scans = 0

            def __iter__(self):
                self.scans += 1
                return super().__iter__()

        with tempfile.TemporaryDirectory() as temporary:
            ir_path, assignment_path, _, net = self._fixture(Path(temporary))
            ir = EmuIR.load(ir_path)
            assignment = read_json(assignment_path)

            def build(count):
                cuts = CountedCuts([net])
                with mock.patch(
                    "emuflow.cut_segment_qualification._cut_nets",
                    return_value=cuts,
                ):
                    artifact = build_cut_segment_qualification_value(
                        ir,
                        assignment,
                        {},
                        path_records=(
                            {"id": f"p{index:04d}", "path_nets": [net, net]}
                            for index in range(count)
                        ),
                    )
                return artifact, cuts.scans

            small, small_scans = build(1)
            large, large_scans = build(1000)

        self.assertEqual(small_scans, large_scans)
        self.assertEqual(
            large["records"][0]["associated_original_path_ids"],
            [f"p{index:04d}" for index in range(1000)],
        )
        self.assertEqual(small["summary"]["enumerated_member_associations"], 1)
        self.assertEqual(large["summary"]["enumerated_member_associations"], 1000)

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
