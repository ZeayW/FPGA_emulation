from tests.partition_fixture import run_phase3_fixture
from contextlib import closing
import copy
import hashlib
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.emuir_compact import write_compact_emuir
from emuflow.io import read_json
from emuflow.ir import EmuIR
from emuflow.partition import (
    build_partition_assignment,
    build_clusters,
    normalize_partition_constraints,
    validate_cluster_assignment_balance,
    validate_partition_artifacts,
    validate_partition_artifacts_online,
)
from tests.partition_reference import assign_clusters
from emuflow.partition_ir import load_partition_ir
from emuflow.phase3 import run_phase3, validate_phase3_indexed
from emuflow.phase3_assignment_index import (
    validate_phase3_assignment_index,
    write_phase3_assignment_index,
)
from emuflow.phase3_exact_records import StaticExactRecordSequence
from emuflow.platform import BoardLink, FpgaNode, Platform
from emuflow.resources import RESOURCE_FIELDS, ResourceVector
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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
        assignment = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=7,
        )
        return constraints, clusters, assignment

    def test_compact_cluster_builder_matches_reference_without_graph_decode(
        self,
    ) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "groups": [
                    {
                        "id": "low_bits",
                        "instances": ["q_reg[0]", "q_reg[1]"],
                    }
                ],
                "fixed": [
                    {"fpga": "fpga1", "instance": "q_reg[0]"}
                ],
            },
            self.ir,
            self.platform,
        )
        expected = build_clusters(self.ir, constraints, cut_mode="sequential-only")
        with tempfile.TemporaryDirectory() as temporary_directory:
            ir_path = Path(temporary_directory) / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            compact_ir = load_partition_ir(ir_path)
            with patch(
                "emuflow.partition_ir._CompactPartitionInstances.__iter__",
                side_effect=AssertionError("cell dictionaries were decoded"),
            ), patch(
                "emuflow.partition_ir._CompactPartitionNets.__iter__",
                side_effect=AssertionError("net dictionaries were decoded"),
            ):
                actual = build_clusters(compact_ir, constraints, cut_mode="sequential-only")
            expected_exact = build_clusters(
                self.ir,
                constraints,
                cut_mode="static-exact-combinational",
            )
            actual_exact = build_clusters(
                compact_ir,
                constraints,
                cut_mode="static-exact-combinational",
            )
            streamed_exact = build_clusters(
                compact_ir,
                constraints,
                cut_mode="static-exact-combinational",
                _stream_eligible_policy_names=True,
            )
            packed_clusters = actual["clusters"]
            self.assertIsInstance(packed_clusters, list)
            self.assertEqual(list.__len__(packed_clusters), 0)
            self.assertGreater(packed_clusters.packed_payload_bytes, 0)
        self.assertEqual(actual, expected)
        self.assertEqual(list(actual["clusters"]), expected["clusters"])
        self.assertEqual(actual["clusters"][0], expected["clusters"][0])
        self.assertEqual(actual["clusters"][-1], expected["clusters"][-1])
        self.assertEqual(
            json.dumps(actual, sort_keys=True, separators=(",", ":")),
            json.dumps(expected, sort_keys=True, separators=(",", ":")),
        )
        copied_clusters = copy.deepcopy(actual)["clusters"]
        self.assertIs(type(copied_clusters), list)
        self.assertEqual(copied_clusters, expected["clusters"])
        self.assertEqual(actual_exact, expected_exact)
        streamed_names = streamed_exact["policy"][
            "eligible_combinational_cut_nets"
        ]
        self.assertIsInstance(streamed_names, list)
        self.assertEqual(list.__len__(streamed_names), 0)
        self.assertEqual(streamed_exact, expected_exact)
        self.assertEqual(
            json.dumps(streamed_exact, sort_keys=True, separators=(",", ":")),
            json.dumps(expected_exact, sort_keys=True, separators=(",", ":")),
        )
        copied = copy.deepcopy(streamed_exact)
        self.assertIs(
            type(copied["policy"]["eligible_combinational_cut_nets"]),
            list,
        )
        self.assertEqual(copied, expected_exact)
        streamed_assignment = assign_clusters(
            self.ir,
            self.platform,
            streamed_exact,
            constraints,
            seed=3,
        )
        self.assertEqual(
            validate_partition_artifacts(
                self.ir,
                self.platform,
                streamed_exact,
                streamed_assignment,
            )["status"],
            "pass",
        )

    def test_compact_cluster_builder_decodes_each_resource_payload_once(
        self,
    ) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        expected = build_clusters(self.ir, constraints, cut_mode="sequential-only")
        with tempfile.TemporaryDirectory() as temporary_directory:
            ir_path = Path(temporary_directory) / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with closing(sqlite3.connect(ir_path)) as connection, connection:
                unique_payloads = connection.execute(
                    "SELECT COUNT(DISTINCT resources_seq) FROM cell_records"
                ).fetchone()[0]
                cell_count = connection.execute(
                    "SELECT COUNT(*) FROM cell_records"
                ).fetchone()[0]
            self.assertLess(unique_payloads, cell_count)
            compact_ir = load_partition_ir(ir_path)
            with patch(
                "emuflow.partition.ResourceVector.from_mapping",
                wraps=ResourceVector.from_mapping,
            ) as decode:
                actual = build_clusters(compact_ir, constraints, cut_mode="sequential-only")
        self.assertEqual(actual, expected)
        self.assertEqual(decode.call_count, unique_payloads)

    def test_compact_default_constraints_skip_instance_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            ir_path = Path(temporary_directory) / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            compact_ir = load_partition_ir(ir_path)
            with patch(
                "emuflow.partition_ir._CompactPartitionInstances.__iter__",
                side_effect=AssertionError(
                    "default constraints must not scan every instance"
                ),
            ):
                constraints = normalize_partition_constraints(
                    None,
                    compact_ir,
                    self.platform,
                    min_used_fpgas=2,
                )
        self.assertEqual(constraints["groups"], [])
        self.assertEqual(constraints["fixed"], [])
        self.assertEqual(constraints["active_fpgas"], ["fpga0", "fpga1"])

    def test_compact_cluster_builder_rejects_invalid_endpoint_owner(self) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            ir_path = Path(temporary_directory) / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with closing(sqlite3.connect(ir_path)) as connection, connection:
                endpoint = connection.execute(
                    """SELECT net_seq,role,ordinal FROM endpoint_records
                        WHERE instance_seq IS NOT NULL LIMIT 1"""
                ).fetchone()
                self.assertIsNotNone(endpoint)
                connection.execute(
                    """UPDATE endpoint_records SET instance_seq=?
                        WHERE net_seq=? AND role=? AND ordinal=?""",
                    (2**31, *endpoint),
                )
            compact_ir = load_partition_ir(ir_path)
            with self.assertRaisesRegex(
                ValidationError, "endpoint owner is invalid"
            ):
                build_clusters(compact_ir, constraints, cut_mode="sequential-only")

    def test_compact_cluster_builder_rejects_missing_resource_payload(
        self,
    ) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            ir_path = Path(temporary_directory) / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with closing(sqlite3.connect(ir_path)) as connection, connection:
                missing_seq = connection.execute(
                    "SELECT coalesce(max(seq), -1) + 1 FROM payloads"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE cell_records SET resources_seq=? WHERE seq=0",
                    (missing_seq,),
                )
            compact_ir = load_partition_ir(ir_path)
            with self.assertRaisesRegex(
                ValidationError, "resource payload is missing"
            ):
                build_clusters(compact_ir, constraints, cut_mode="sequential-only")

    def test_compact_cluster_builder_rejects_non_object_resources(self) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            ir_path = Path(temporary_directory) / "design.emuir.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with closing(sqlite3.connect(ir_path)) as connection, connection:
                resources_seq = connection.execute(
                    "SELECT resources_seq FROM cell_records WHERE seq=0"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE payloads SET value='[123456789]' WHERE seq=?",
                    (resources_seq,),
                )
            compact_ir = load_partition_ir(ir_path)
            with self.assertRaisesRegex(
                ValidationError, "resources: expected an object"
            ):
                build_clusters(compact_ir, constraints, cut_mode="sequential-only")



    def test_static_exact_compact_input_uses_bounded_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            output = root / "phase3"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with patch(
                "emuflow.partition_ir.EmuIR.load",
                side_effect=AssertionError(
                    "Static Exact Phase 3 must stream compact EmuIR"
                ),
            ), patch(
                "emuflow.phase3_publication_native.publish",
                side_effect=AssertionError("Python final publication entered"),
            ):
                run_phase3_fixture(
                    ir_path,
                    PLATFORM_PATH,
                    output,
                    provider="tritonpart",
                    seed=7,
                    cut_mode="static-exact-combinational",
                )
            assignment = read_json(output / "assignment.json")
            expected_assignment_digest = hashlib.sha256((json.dumps({'instance_assignment': dict(assignment['instance_assignment'])}, indent=2, sort_keys=True)+'\n').encode()).hexdigest()
            self.assertEqual(assignment['semantic_contract']['source_identity']['instance_assignment_sha256'], expected_assignment_digest)
            self.assertEqual(read_json(output/'phase3_report.json')['stage_implementation_id'], 'native-phase3-v1')
            packed_assignment = json.loads(
                (output / "assignment.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                packed_assignment["schema"],
                "emuflow.phase3-assignment-storage/v4",
            )
            self.assertNotIn("cut_nets", packed_assignment["document"])
            self.assertEqual(
                packed_assignment["cut_nets_index"]["artifact"],
                "assignment.sqlite",
            )
            self.assertIsNone(packed_assignment["semantic_contract"])
            self.assertEqual(
                packed_assignment["semantic_contract_index"]["artifact"],
                "assignment.sqlite",
            )
            binary_path = output / "assignment.exact.bin"
            self.assertTrue(binary_path.is_file())
            with closing(sqlite3.connect(output / "assignment.sqlite")) as connection, connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE type='table' AND name LIKE 'exact_%'"
                    ).fetchone()[0],
                    0,
                )
            runtime = read_json(output / "phase3-runtime.json")
            self.assertEqual(
                runtime["observations"]["static_exact_native"], 1
            )
            source = assignment["semantic_contract"]["source_identity"]
            self.assertEqual(
                source["emuir"],
                {
                    "schema": "emuflow.emuir/v1",
                    "design": "counter",
                    "instances": len(self.ir.value["instances"]),
                    "nets": len(self.ir.value["nets"]),
                    "qualification": (
                        "phase1-index-plus-assignment-relation-v1"
                    ),
                },
            )
            self.assertNotIn("canonical_emuir_sha256", source)
            encoded = bytearray(binary_path.read_bytes())
            encoded[28] ^= 1
            binary_path.write_bytes(encoded)
            with self.assertRaisesRegex(ValidationError, "Static Exact"):
                validate_phase3_assignment_index(output / "assignment.sqlite")

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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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

    def test_partition_constraints_seal_downstream_resource_reservations(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "resource_reservations_by_fpga": {
                    "fpga1": {"ff": 2, "lut": 1},
                    "fpga0": {"lut": 0},
                },
            },
            self.ir,
            self.platform,
        )
        self.assertEqual(
            constraints["resource_reservations_by_fpga"],
            {"fpga1": {"ff": 2, "lut": 1}},
        )
        invalid_values = [
            {"fpga0": {"lut": True}},
            {"fpga0": {"lut": -1}},
            {"fpga0": {"unknown": 1}},
            {"missing": {"lut": 1}},
            {
                "fpga0": {
                    "lut": self.platform.fpgas[0].effective_capacity["lut"]
                    + 1
                }
            },
        ]
        for reservations in invalid_values:
            with self.subTest(reservations=reservations):
                with self.assertRaises(ValidationError):
                    normalize_partition_constraints(
                        {
                            "schema": "emuflow.partition-constraints/v1",
                            "resource_reservations_by_fpga": reservations,
                        },
                        self.ir,
                        self.platform,
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
        wide_constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fpga_selection_policy": "minimum-capacity",
                "min_used_fpgas": 16,
            },
            self.ir,
            platform,
        )
        self.assertEqual(
            wide_constraints["active_fpgas"],
            [f"fpga{index}" for index in range(16)],
        )

    def test_minimum_capacity_prefers_well_connected_homogeneous_subset(
        self,
    ) -> None:
        total = ResourceVector.sum(
            ResourceVector.from_mapping(instance["resources"])
            for instance in self.ir.value["instances"]
        ).to_dict()
        limiting_resource = next(
            field for field in RESOURCE_FIELDS if total.get(field, 0) > 1
        )
        capacity = {field: max(total.get(field, 0), 1) for field in RESOURCE_FIELDS}
        capacity[limiting_resource] = math.ceil(
            total[limiting_resource] / 2
        )
        fpgas = tuple(
            FpgaNode(
                id=f"fpga{index}",
                part="homogeneous-test-part",
                utilization_limit=1.0,
                capacity=dict(capacity),
            )
            for index in range(4)
        )
        links = (
            BoardLink(
                id="link-0-2",
                endpoints=("fpga0", "fpga2"),
                direction="full_duplex",
                mode="abstract",
                data_lanes_per_direction=64,
                fabric_clock_mhz=250.0,
                latency_cycles=2,
            ),
            BoardLink(
                id="link-1-3",
                endpoints=("fpga1", "fpga3"),
                direction="full_duplex",
                mode="abstract",
                data_lanes_per_direction=64,
                fabric_clock_mhz=250.0,
                latency_cycles=2,
            ),
        )
        platform = Platform(
            name="homogeneous_disconnected_pairs",
            kind="virtual",
            description="topology-aware selection fixture",
            fpgas=fpgas,
            links=links,
        )
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fpga_selection_policy": "minimum-capacity",
            },
            self.ir,
            platform,
        )
        self.assertEqual(
            constraints["active_fpgas"], ["fpga0", "fpga2"]
        )

    def test_minimum_capacity_prefers_endpoint_boundary_headroom(self) -> None:
        total = ResourceVector.sum(
            ResourceVector.from_mapping(instance["resources"])
            for instance in self.ir.value["instances"]
        ).to_dict()
        limiting_resource = next(
            field for field in RESOURCE_FIELDS if total.get(field, 0) > 1
        )
        capacity = {
            field: max(total.get(field, 0), 1) for field in RESOURCE_FIELDS
        }
        capacity[limiting_resource] = math.ceil(
            total[limiting_resource] / 2
        )
        fpgas = tuple(
            FpgaNode(
                id=f"fpga{index}",
                part="asymmetric-boundary-test-part",
                utilization_limit=1.0,
                capacity=dict(capacity),
            )
            for index in range(4)
        )

        def directed(source: int, sink: int, width: int) -> BoardLink:
            return BoardLink(
                id=f"link-{source}-{sink}",
                endpoints=(f"fpga{source}", f"fpga{sink}"),
                direction="unidirectional",
                mode="abstract",
                data_lanes_per_direction=width,
                fabric_clock_mhz=250.0,
                latency_cycles=2,
            )

        # fpga0/fpga2 and fpga1/fpga2 have the same direct-pair capacity.
        # Only the latter pair has enough ingress and egress at both active
        # endpoints once inactive devices are allowed to act as relays.
        links = (
            directed(0, 2, 64),
            directed(2, 0, 128),
            directed(0, 3, 64),
            directed(3, 0, 128),
            directed(1, 2, 128),
            directed(2, 1, 64),
            directed(1, 3, 64),
            directed(3, 1, 128),
        )
        platform = Platform(
            name="asymmetric_endpoint_boundaries",
            kind="virtual",
            description="endpoint-cut-aware selection fixture",
            fpgas=fpgas,
            links=links,
        )
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "fpga_selection_policy": "minimum-capacity",
            },
            self.ir,
            platform,
        )
        self.assertEqual(constraints["active_fpgas"], ["fpga1", "fpga2"])

    def test_single_active_target_skips_requested_patron(self) -> None:
        for compact in (True,):
            with self.subTest(compact=compact), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                ir_path = root / ("design.emuir.sqlite" if compact else "design.emuir.json")
                constraints_path = root / "constraints.json"
                if compact:
                    write_compact_emuir(ir_path, self.ir.to_dict())
                else:
                    ir_path.write_text(json.dumps(self.ir.to_dict()), encoding="utf-8")
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
                    patch("emuflow.partition_init_native.initialize") as tritonpart,
                    patch(
                        "emuflow.partition_pressure.run_partition_pressure_native"
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
                requested_balance_dimensions=("cells", "bram"),
            )
        report = validate_cluster_assignment_balance(
            self.platform,
            clusters,
            assignment,
            requested_tolerance=0.25,
            requested_tolerance_by_dimension={"bram": 0.50},
            requested_balance_dimensions=("cells", "bram"),
        )
        self.assertEqual(
            report["requested_balance_percent_by_dimension"]["cells"], 25.0
        )
        self.assertEqual(
            report["requested_balance_percent_by_dimension"]["bram"], 50.0
        )

    def test_balance_validation_consumes_cluster_records_once(self) -> None:
        class SinglePassClusters(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                if self.iterations > 1:
                    raise AssertionError("cluster records were scanned twice")
                return super().__iter__()

        clusters = SinglePassClusters(
            [
                {
                    "id": f"cluster_{index}",
                    "instances": [f"instance_{index}"],
                    "resources": {"lut": 1, "ff": 1},
                    "fixed_fpga": None,
                }
                for index in range(4)
            ]
        )
        report = validate_cluster_assignment_balance(
            self.platform,
            clusters,
            {
                "cluster_0": "fpga0",
                "cluster_1": "fpga0",
                "cluster_2": "fpga1",
                "cluster_3": "fpga1",
            },
            requested_tolerance=0.25,
        )
        self.assertEqual(clusters.iterations, 1)
        self.assertEqual(report["balance_loads"]["fpga0"]["cells"], 2)
        self.assertEqual(report["balance_loads"]["fpga1"]["cells"], 2)

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

    def test_default_balance_dimensions_enforce_logic_balance(self) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        self.assertEqual(
            constraints["balance_dimensions"], ["cells", "ff", "lut"]
        )

    def test_tolerance_override_preserves_default_logic_dimensions(self) -> None:
        constraints = normalize_partition_constraints(
            {"schema": "emuflow.partition-constraints/v1", "balance_tolerance": 0.0},
            self.ir, self.platform, balance_tolerance=0.10,
        )
        self.assertEqual(
            constraints["balance_dimensions"], ["cells", "ff", "lut"]
        )
        self.assertEqual(constraints["balance_tolerance"], 0.10)

    def test_balance_dimensions_are_explicit_and_validated(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_dimensions": ["cells", "lut"],
            },
            self.ir,
            self.platform,
        )
        self.assertEqual(constraints["balance_dimensions"], ["cells", "lut"])
        with self.assertRaisesRegex(ValidationError, "unknown dimensions"):
            normalize_partition_constraints(
                {
                    "schema": "emuflow.partition-constraints/v1",
                    "balance_dimensions": ["cells", "unknown"],
                },
                self.ir,
                self.platform,
            )


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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
        with self.assertRaisesRegex(ValidationError, "cannot fit any FPGA"):
            assign_clusters(
                self.ir,
                platform,
                clusters,
                constraints,
            )

    def test_capacity_only_assignment_and_sealed_legacy_balance(self) -> None:
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "min_used_fpgas": 1,
                "balance_tolerance": 0.10,
                "balance_dimensions": [],
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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
        checked = validate_partition_artifacts(self.ir, self.platform, clusters, assignment)
        self.assertEqual(checked["strict_balance_dimensions"], [])
        self.assertEqual(checked["balance_observation"]["max_to_mean_by_dimension"]["cells"], 2.0)
        # Explicit opt-in is still strict, as is a historical v1 artifact whose
        # sealed constraints predate the balance_dimensions field.
        for dimensions in (["cells", "lut", "ff"], None):
            historical = copy.deepcopy(assignment)
            if dimensions is None:
                historical["constraints"].pop("balance_dimensions")
            else:
                historical["constraints"]["balance_dimensions"] = dimensions
            with self.assertRaisesRegex(ValidationError, "multi-resource balance"):
                validate_partition_artifacts(self.ir, self.platform, clusters, historical)
            with self.assertRaisesRegex(ValidationError, "multi-resource balance"):
                validate_partition_artifacts_online(self.platform, clusters, historical)

if __name__ == "__main__":
    unittest.main()
