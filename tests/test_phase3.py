import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.ir import EmuIR
from emuflow.partition import (
    assign_clusters,
    build_partition_assignment,
    build_clusters,
    normalize_partition_constraints,
    validate_cluster_assignment_balance,
    validate_partition_artifacts,
)
from emuflow.phase3 import run_phase3
from emuflow.platform import FpgaNode, Platform
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


class Phase3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        self.platform = Platform.load(PLATFORM_PATH)

    def _artifacts(self):
        constraints = normalize_partition_constraints(
            None,
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        assignment = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=7,
        )
        return constraints, clusters, assignment

    def test_minimum_capacity_policy_uses_one_partition_target(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fpga_selection_policy": "minimum-capacity",
            },
            self.ir,
            self.platform,
        )
        self.assertEqual(constraints["active_fpgas"], ["fpga0"])
        self.assertEqual(constraints["min_used_fpgas"], 1)
        clusters = build_clusters(self.ir, constraints)
        assignment = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=7,
        )
        self.assertEqual(
            set(assignment["instance_assignment"].values()), {"fpga0"}
        )
        report = validate_partition_artifacts(
            self.ir, self.platform, clusters, assignment
        )
        self.assertEqual(report["used_fpgas"], 1)
        tampered = copy.deepcopy(constraints)
        tampered["active_fpgas"] = ["fpga0", "fpga1"]
        with self.assertRaisesRegex(
            ValidationError, "minimum-capacity selection"
        ):
            normalize_partition_constraints(
                tampered, self.ir, self.platform
            )

    def test_minimum_capacity_scales_to_homogeneous_32_fpga_platform(
        self,
    ) -> None:
        template = self.platform.fpgas[0]
        platform = Platform(
            name="homogeneous_32fpga",
            kind="virtual",
            description="selection scalability fixture",
            fpgas=tuple(
                FpgaNode(
                    id=f"fpga{index}",
                    part=template.part,
                    utilization_limit=template.utilization_limit,
                    capacity=dict(template.capacity),
                )
                for index in range(32)
            ),
            links=(),
        )
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fpga_selection_policy": "minimum-capacity",
                "fixed": [
                    {"instance": "q_reg[0]", "fpga": "fpga31"}
                ],
            },
            self.ir,
            platform,
        )
        self.assertEqual(constraints["active_fpgas"], ["fpga31"])

    def test_single_active_target_skips_requested_patron(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            constraints_path = root / "constraints.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            constraints_path.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.partition-constraints/v1",
                        "fpga_selection_policy": "minimum-capacity",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("emuflow.phase3.run_tritonpart") as tritonpart,
                patch(
                    "emuflow.phase3.run_partition_pressure_native"
                ) as patron,
            ):
                report = run_phase3(
                    ir_path=ir_path,
                    platform_path=PLATFORM_PATH,
                    output_dir=root / "phase3",
                    constraints_path=constraints_path,
                    provider="patron",
                    seed=19,
                )
            tritonpart.assert_not_called()
            patron.assert_not_called()
            self.assertEqual(
                report["provider"],
                "single-active-fpga-capacity-selection-v1",
            )
            self.assertEqual(report["requested_provider"], "patron")
            self.assertFalse(report["partition_decision"]["executed"])
            self.assertEqual(report["validation"]["used_fpgas"], 1)

    def test_explicit_active_subset_rejects_inactive_fixed_instance(self) -> None:
        with self.assertRaisesRegex(ValidationError, "inactive FPGAs"):
            normalize_partition_constraints(
                {
                    "schema": "emuflow.partition-constraints/v1",
                    "active_fpgas": ["fpga0"],
                    "fixed": [
                        {"instance": "q_reg[0]", "fpga": "fpga1"}
                    ],
                },
                self.ir,
                self.platform,
            )

    def test_dimension_specific_balance_tolerance_preserves_tight_cell_balance(
        self,
    ) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 0.25,
                "balance_tolerance_by_dimension": {"bram": 0.50},
            },
            self.ir,
            self.platform,
        )
        self.assertEqual(
            constraints["balance_tolerance_by_dimension"], {"bram": 0.50}
        )
        clusters = []
        brams = [2, 2, 1, 1, 1, 1, 0, 0]
        assignment = {}
        for index, bram in enumerate(brams):
            cluster_id = f"cluster_{index}"
            clusters.append(
                {
                    "id": cluster_id,
                    "instances": [f"instance_{index}"],
                    "resources": {"bram": bram},
                    "fixed_fpga": None,
                }
            )
            assignment[cluster_id] = "fpga0" if index < 4 else "fpga1"
        with self.assertRaisesRegex(ValidationError, "multi-resource balance"):
            validate_cluster_assignment_balance(
                self.platform,
                clusters,
                assignment,
                requested_tolerance=0.25,
            )
        report = validate_cluster_assignment_balance(
            self.platform,
            clusters,
            assignment,
            requested_tolerance=0.25,
            requested_tolerance_by_dimension={"bram": 0.50},
        )
        self.assertEqual(
            report["requested_balance_percent_by_dimension"]["cells"], 25.0
        )
        self.assertEqual(
            report["requested_balance_percent_by_dimension"]["bram"], 50.0
        )

    def test_dimension_specific_balance_rejects_unknown_resource(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown dimensions"):
            normalize_partition_constraints(
                {
                    "schema": "emuflow.partition-constraints/v1",
                    "balance_tolerance_by_dimension": {"unknown": 1.0},
                },
                self.ir,
                self.platform,
            )

    def test_pipeline_writes_valid_forced_two_fpga_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            ir_path = output / "design.emuir.json"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            report = run_phase3(
                ir_path=ir_path,
                platform_path=PLATFORM_PATH,
                output_dir=output / "phase3",
                seed=11,
                provider="greedy",
                cut_mode="sequential-only",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["validation"]["instances"], 8)
            self.assertEqual(report["validation"]["used_fpgas"], 2)
            self.assertEqual(report["validation"]["illegal_cuts"], 0)
            self.assertGreater(report["validation"]["cut_nets"], 0)
            self.assertTrue(
                all(
                    "clusters" not in partition
                    for partition in report["partitions"]
                )
            )
            for filename in (
                "clusters.json",
                "constraints.normalized.json",
                "assignment.json",
                "phase3_report.json",
            ):
                self.assertTrue((output / "phase3" / filename).is_file())

    def test_assignment_is_reproducible_for_fixed_seed(self) -> None:
        constraints, clusters, first = self._artifacts()
        second = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=7,
        )
        self.assertEqual(first, second)

    def test_group_and_fixed_constraints_hold(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "groups": [
                    {
                        "id": "low_bits",
                        "patterns": ["q_reg[[]0]", "q_reg[[]1]"],
                    }
                ],
                "fixed": [
                    {
                        "fpga": "fpga1",
                        "patterns": ["q_reg[[]0]"],
                    }
                ],
                "min_used_fpgas": 2,
                "balance_tolerance": 0.50,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        assignment = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=3,
        )
        validation = validate_partition_artifacts(
            self.ir,
            self.platform,
            clusters,
            assignment,
        )
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(
            assignment["instance_assignment"]["q_reg[0]"], "fpga1"
        )
        self.assertEqual(
            assignment["instance_assignment"]["q_reg[1]"], "fpga1"
        )

    def test_missing_instance_is_rejected(self) -> None:
        _, clusters, assignment = self._artifacts()
        broken = copy.deepcopy(assignment)
        broken["instance_assignment"].pop("q_reg[0]")
        with self.assertRaisesRegex(ValidationError, "exact coverage failed"):
            validate_partition_artifacts(
                self.ir,
                self.platform,
                clusters,
                broken,
            )

    def test_cluster_split_is_rejected(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "groups": [
                    {
                        "id": "paired_cells",
                        "instances": ["next_lut[0]", "q_reg[0]"],
                    }
                ],
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        assignment = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=7,
        )
        broken = copy.deepcopy(assignment)
        cluster = next(
            cluster
            for cluster in clusters["clusters"]
            if len(cluster["instances"]) > 1
        )
        instance_id = cluster["instances"][0]
        old_fpga = broken["instance_assignment"][instance_id]
        broken["instance_assignment"][instance_id] = (
            "fpga1" if old_fpga == "fpga0" else "fpga0"
        )
        with self.assertRaisesRegex(ValidationError, "spans FPGAs"):
            validate_partition_artifacts(
                self.ir,
                self.platform,
                clusters,
                broken,
            )

    def test_register_input_cut_uses_second_transport_round(self) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        clusters = build_clusters(self.ir, constraints)
        cluster_by_instance = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        cluster_assignment = {
            cluster["id"]: "fpga0" for cluster in clusters["clusters"]
        }
        cluster_assignment[cluster_by_instance["next_lut[0]"]] = "fpga1"
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_assignment,
            provider="test",
            seed=0,
        )
        cut_by_net = {
            cut["net"]: cut for cut in assignment["cut_nets"]
        }
        self.assertEqual(
            cut_by_net["next_q[0]"]["cut_class"], "register_input"
        )
        self.assertEqual(cut_by_net["next_q[0]"]["transport_round"], 1)
        self.assertNotIn("transport_round", cut_by_net["q[0]"])
        self.assertEqual(
            assignment["metrics"]["register_input_cut_nets"], 1
        )
        self.assertEqual(assignment["metrics"]["transport_rounds"], 2)
        self.assertEqual(assignment["metrics"]["round_barriers"], 1)

    def test_infeasible_capacity_is_rejected(self) -> None:
        platform_value = self.platform.to_dict()
        for fpga in platform_value["fpgas"]:
            fpga["utilization_limit"] = 1.0
            fpga["capacity"]["lut"] = 1
            fpga["capacity"]["ff"] = 1
        platform = Platform.from_dict(platform_value)
        constraints = normalize_partition_constraints(
            None,
            self.ir,
            platform,
        )
        clusters = build_clusters(self.ir, constraints)
        with self.assertRaisesRegex(ValidationError, "cannot fit any FPGA"):
            assign_clusters(
                self.ir,
                platform,
                clusters,
                constraints,
            )

    def test_unbalanced_provider_assignment_is_rejected(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 1,
                "balance_tolerance": 0.10,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints)
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            {
                cluster["id"]: "fpga0"
                for cluster in clusters["clusters"]
            },
            provider="deliberately-unbalanced-test",
            seed=0,
        )
        with self.assertRaisesRegex(
            ValidationError, "multi-resource balance"
        ):
            validate_partition_artifacts(
                self.ir,
                self.platform,
                clusters,
                assignment,
            )

if __name__ == "__main__":
    unittest.main()
