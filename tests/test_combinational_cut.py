import copy
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import main
from emuflow.combinational_cut import (
    GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA,
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    build_static_exact_semantic_contract,
    characterize_combinational_cuts,
    semantic_contract_sha256,
    validate_combinational_cut_characterization,
)
from emuflow.errors import ValidationError
from emuflow.equivalence import (
    _MappedModel,
    exhaustively_verify_static_exact_partition_equivalence,
    simulate_static_exact_partition_equivalence,
)
from emuflow.ir import EmuIR
from emuflow.netlist import (
    build_split_artifacts,
    transport_to_systemverilog,
    validate_split_artifacts,
)
from emuflow.partition import (
    CUT_MODE_STATIC_EXACT,
    assign_clusters,
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
    validate_partition_artifacts,
)
from emuflow.platform import Platform
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.routing import (
    demands_from_assignment,
    normalize_route_constraints,
    validate_system_routes,
)
from emuflow.tdm import (
    TDM_STATIC_EXACT_CERTIFICATE_SCHEMA,
    TDM_STATIC_EXACT_PROVIDER,
    build_tdm_schedule,
    validate_tdm_schedule,
)
from emuflow.timing_routing import route_system_native


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


def _endpoint(instance, port):
    return {"instance": instance, "port": port, "bit": 0}


def _chain_ir():
    instances = [
        {"id": "q0", "type": "FDRE", "resources": {"ff": 1}},
        {
            "id": "l0",
            "type": "LUT2",
            "parameters": {"INIT": "1010"},
            "resources": {"lut": 1},
        },
        {
            "id": "l1",
            "type": "LUT2",
            "parameters": {"INIT": "1010"},
            "resources": {"lut": 1},
        },
        {
            "id": "l2",
            "type": "LUT2",
            "parameters": {"INIT": "1010"},
            "resources": {"lut": 1},
        },
        {"id": "q1", "type": "FDRE", "resources": {"ff": 1}},
    ]
    nets = [
        {
            "id": "q",
            "name": "q",
            "cut_class": "register_output",
            "drivers": [_endpoint("q0", "Q")],
            "sinks": [_endpoint("l0", "I0")],
        },
        {
            "id": "n0",
            "name": "n0",
            "cut_class": "combinational",
            "drivers": [_endpoint("l0", "O")],
            "sinks": [_endpoint("l1", "I0")],
        },
        {
            "id": "n1",
            "name": "n1",
            "cut_class": "combinational",
            "drivers": [_endpoint("l1", "O")],
            "sinks": [_endpoint("l2", "I0")],
        },
        {
            "id": "d",
            "name": "d",
            "cut_class": "register_input",
            "drivers": [_endpoint("l2", "O")],
            "sinks": [_endpoint("q1", "D")],
        },
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {"name": "cut_chain", "top": "cut_chain", "source_format": "test"},
            "ports": [],
            "instances": instances,
            "nets": nets,
            "clocks": [
                {
                    "id": "clk",
                    "name": "clk",
                    "source_port": "clk",
                    "period_ns": None,
                }
            ],
            "warnings": [],
        }
    )


def _wide_fanout_ir(width=32):
    instances = [
        {"id": "q0", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "source_lut", "type": "LUT2", "resources": {"lut": 1}},
    ]
    nets = [
        {
            "id": "q",
            "name": "q",
            "cut_class": "register_output",
            "drivers": [_endpoint("q0", "Q")],
            "sinks": [_endpoint("source_lut", "I0")],
        }
    ]
    fanout_sinks = []
    for index in range(width):
        lut = f"sink_lut_{index:03d}"
        register = f"sink_ff_{index:03d}"
        instances.extend(
            [
                {"id": lut, "type": "LUT2", "resources": {"lut": 1}},
                {"id": register, "type": "FDRE", "resources": {"ff": 1}},
            ]
        )
        fanout_sinks.append(_endpoint(lut, "I0"))
        nets.append(
            {
                "id": f"d_{index:03d}",
                "name": f"d_{index:03d}",
                "cut_class": "register_input",
                "drivers": [_endpoint(lut, "O")],
                "sinks": [_endpoint(register, "D")],
            }
        )
    nets.append(
        {
            "id": "wide_boundary",
            "name": "wide_boundary",
            "cut_class": "combinational",
            "drivers": [_endpoint("source_lut", "O")],
            "sinks": fanout_sinks,
        }
    )
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "wide_fanout",
                "top": "wide_fanout",
                "source_format": "test",
            },
            "ports": [],
            "instances": instances,
            "nets": nets,
            "clocks": [
                {
                    "id": "clk",
                    "name": "clk",
                    "source_port": "clk",
                    "period_ns": None,
                }
            ],
            "warnings": [],
        }
    )


def _reconvergent_ir():
    instances = [
        {"id": "qa", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "qb", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "qc", "type": "FDRE", "resources": {"ff": 1}},
        {"id": "la", "type": "LUT2", "resources": {"lut": 1}},
        {"id": "lb", "type": "LUT2", "resources": {"lut": 1}},
        {
            "id": "merge",
            "type": "LUT3",
            "parameters": {"INIT": "10000000"},
            "resources": {"lut": 1},
        },
        {"id": "after", "type": "LUT2", "resources": {"lut": 1}},
        {"id": "qout", "type": "FDRE", "resources": {"ff": 1}},
    ]
    nets = [
        {
            "id": "qa_q",
            "name": "qa_q",
            "cut_class": "register_output",
            "drivers": [_endpoint("qa", "Q")],
            "sinks": [_endpoint("la", "I0")],
        },
        {
            "id": "qb_q",
            "name": "qb_q",
            "cut_class": "register_output",
            "drivers": [_endpoint("qb", "Q")],
            "sinks": [_endpoint("lb", "I0")],
        },
        {
            "id": "a",
            "name": "a",
            "cut_class": "combinational",
            "drivers": [_endpoint("la", "O")],
            "sinks": [_endpoint("merge", "I0")],
        },
        {
            "id": "b",
            "name": "b",
            "cut_class": "combinational",
            "drivers": [_endpoint("lb", "O")],
            "sinks": [_endpoint("merge", "I1")],
        },
        {
            "id": "qc_q",
            "name": "qc_q",
            "cut_class": "register_output",
            "drivers": [_endpoint("qc", "Q")],
            "sinks": [_endpoint("merge", "I2")],
        },
        {
            "id": "c",
            "name": "c",
            "cut_class": "combinational",
            "drivers": [_endpoint("merge", "O")],
            "sinks": [_endpoint("after", "I0")],
        },
        {
            "id": "d",
            "name": "d",
            "cut_class": "register_input",
            "drivers": [_endpoint("after", "O")],
            "sinks": [_endpoint("qout", "D")],
        },
    ]
    return EmuIR(
        {
            "schema": "emuflow.emuir/v1",
            "design": {
                "name": "reconvergent_cut",
                "top": "reconvergent_cut",
                "source_format": "test",
            },
            "ports": [],
            "instances": instances,
            "nets": nets,
            "clocks": [
                {
                    "id": "clk",
                    "name": "clk",
                    "source_port": "clk",
                    "period_ns": None,
                }
            ],
            "warnings": [],
        }
    )


def _three_fpga_platform(topology="line"):
    value = json.loads(PLATFORM_PATH.read_text(encoding="utf-8"))
    value["platform"]["name"] = f"static_exact_three_fpga_{topology}"
    fpga2 = copy.deepcopy(value["fpgas"][1])
    fpga2["id"] = "fpga2"
    value["fpgas"].append(fpga2)
    if topology == "line":
        value["links"].append(
            {
                **copy.deepcopy(value["links"][0]),
                "id": "link_1_2",
                "endpoints": ["fpga1", "fpga2"],
            }
        )
    elif topology == "star":
        value["links"].append(
            {
                **copy.deepcopy(value["links"][0]),
                "id": "link_0_2",
                "endpoints": ["fpga0", "fpga2"],
            }
        )
    else:
        raise AssertionError("unknown fixture topology")
    return Platform.from_dict(value)


class CombinationalCutCharacterizationTest(unittest.TestCase):
    def test_canonical_hash_streams_the_legacy_pretty_json_identity(self):
        value = {
            "z": [1, {"unicode": "π", "enabled": True}],
            "a": {"nested": None},
        }
        expected = hashlib.sha256(
            (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        ).hexdigest()
        with patch(
            "emuflow.combinational_cut.json.dumps",
            side_effect=AssertionError("canonical hashing must stream"),
        ):
            self.assertEqual(semantic_contract_sha256(value), expected)

    def test_mapped_model_evaluates_reverse_named_chain_once_per_cell(self):
        width = 256
        instance_ids = [f"lut_{index:04d}" for index in reversed(range(width))]
        nets = [
            {
                "id": "input_net",
                "name": "input_net",
                "cut_class": "combinational",
                "drivers": [_endpoint(None, "input")],
                "sinks": [_endpoint(instance_ids[0], "I0")],
            }
        ]
        for index, instance_id in enumerate(instance_ids):
            sink = (
                _endpoint(instance_ids[index + 1], "I0")
                if index + 1 < width
                else _endpoint(None, "output")
            )
            nets.append(
                {
                    "id": f"net_{index:04d}",
                    "name": f"net_{index:04d}",
                    "cut_class": "combinational",
                    "drivers": [_endpoint(instance_id, "O")],
                    "sinks": [sink],
                }
            )
        model = _MappedModel(
            EmuIR(
                {
                    "schema": "emuflow.emuir/v1",
                    "design": {
                        "name": "reverse_named_chain",
                        "top": "reverse_named_chain",
                        "source_format": "test",
                    },
                    "ports": [
                        {"id": "input", "direction": "input", "width": 1},
                        {"id": "output", "direction": "output", "width": 1},
                    ],
                    "instances": [
                        {
                            "id": instance_id,
                            "type": "LUT1",
                            "parameters": {"INIT": "10"},
                            "resources": {"lut": 1},
                        }
                        for instance_id in instance_ids
                    ],
                    "nets": nets,
                    "clocks": [],
                    "warnings": [],
                }
            )
        )
        evaluations = []
        original = model._evaluate_combinational_instance

        def counted(values, instance_id, overrides=None):
            evaluations.append(instance_id)
            return original(values, instance_id, overrides)

        model._evaluate_combinational_instance = counted
        values, _, outputs = model.evaluate({}, 0, 17)
        self.assertEqual(evaluations, instance_ids)
        self.assertEqual(len(evaluations), width)
        self.assertIn(f"net_{width - 1:04d}", values)
        self.assertEqual(outputs, {"output[0]": values[f"net_{width - 1:04d}"]})

    def test_sparse_graph_membership_is_not_quadratic(self):
        class CountingString(str):
            comparisons = 0

            def __eq__(self, other):
                type(self).comparisons += 1
                return super().__eq__(other)

            __hash__ = str.__hash__

        instance_ids = [
            CountingString(f"lut_{index:04d}") for index in range(256)
        ]
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "sparse_membership",
                    "top": "sparse_membership",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {"id": instance_id, "type": "LUT2", "resources": {"lut": 1}}
                    for instance_id in instance_ids
                ],
                "nets": [
                    {
                        "id": CountingString(f"net_{index:04d}"),
                        "name": f"net_{index:04d}",
                        "cut_class": "combinational",
                        "drivers": [_endpoint(instance_id, "O")],
                        "sinks": [],
                    }
                    for index, instance_id in enumerate(instance_ids)
                ],
                "clocks": [],
                "warnings": [],
            }
        )
        CountingString.comparisons = 0
        report = characterize_combinational_cuts(ir, (1,))
        self.assertEqual(report["metrics"]["instances"], len(instance_ids))
        self.assertLess(CountingString.comparisons, len(instance_ids))

    def test_chain_has_stable_dependency_depth_and_split_upper_bounds(self):
        ir = _chain_ir()
        report = characterize_combinational_cuts(ir)
        self.assertFalse(report["behavior_change"])
        cuts = {item["net"]: item for item in report["eligible_cuts"]}
        self.assertEqual(cuts["n0"]["dependency_level"], 1)
        self.assertEqual(cuts["n0"]["predecessor_cut_nets"], [])
        self.assertEqual(cuts["n1"]["dependency_level"], 2)
        self.assertEqual(cuts["n1"]["predecessor_cut_nets"], ["n0"])
        self.assertEqual(
            report["current_sequential_only_atomic_components"]["maximum_instances"],
            3,
        )
        by_limit = {
            item["max_dependency_depth"]: item for item in report["depth_limits"]
        }
        self.assertEqual(by_limit[1]["atomic_components"]["maximum_instances"], 2)
        self.assertEqual(by_limit[2]["atomic_components"]["maximum_instances"], 1)
        self.assertEqual(
            validate_combinational_cut_characterization(ir, report)["status"],
            "pass",
        )

    def test_cycle_is_fail_closed_and_stays_atomic(self):
        value = _chain_ir().to_dict()
        value["nets"][2]["sinks"].append(_endpoint("l0", "I1"))
        ir = EmuIR(value)
        report = characterize_combinational_cuts(ir)
        self.assertEqual(report["metrics"]["cyclic_combinational_sccs"], 1)
        self.assertEqual(report["eligible_cuts"], [])
        reasons = {
            item["net"]: item["reasons"]
            for item in report["ineligible_combinational_cuts"]
        }
        self.assertIn("driver-in-combinational-cycle", reasons["n0"])
        self.assertIn("sink-in-combinational-cycle", reasons["n1"])

    def test_opaque_driver_is_not_eligible(self):
        value = _chain_ir().to_dict()
        instance = next(item for item in value["instances"] if item["id"] == "l0")
        instance["type"] = "VTR_MULTIPLY"
        instance["resources"] = {"dsp": 1}
        report = characterize_combinational_cuts(EmuIR(value))
        reasons = {
            item["net"]: item["reasons"]
            for item in report["ineligible_combinational_cuts"]
        }
        self.assertIn("driver-not-supported-soft-logic", reasons["n0"])

    def test_multi_driver_async_control_and_latch_sinks_fail_closed(self):
        value = _chain_ir().to_dict()
        value["nets"][1]["drivers"].append(_endpoint("l1", "O"))
        value["nets"][2]["sinks"] = [_endpoint("q1", "CLR")]
        value["instances"].append(
            {"id": "lat", "type": "DLATCH", "resources": {"ff": 1}}
        )
        value["nets"].append(
            {
                "id": "latch_control",
                "name": "latch_control",
                "cut_class": "combinational",
                "drivers": [_endpoint("l2", "O")],
                "sinks": [_endpoint("lat", "G")],
            }
        )
        report = characterize_combinational_cuts(EmuIR(value))
        reasons = {
            item["net"]: item["reasons"]
            for item in report["ineligible_combinational_cuts"]
        }
        self.assertIn("not-single-instance-driver", reasons["n0"])
        self.assertIn(
            "unsupported-sequential-control-or-memory-sink", reasons["n1"]
        )
        self.assertIn(
            "unsupported-sequential-control-or-memory-sink",
            reasons["latch_control"],
        )

    def test_tampered_report_is_rejected(self):
        ir = _chain_ir()
        report = characterize_combinational_cuts(ir)
        tampered = copy.deepcopy(report)
        tampered["eligible_cuts"][1]["dependency_level"] = 1
        with self.assertRaisesRegex(ValidationError, "independent EmuIR"):
            validate_combinational_cut_characterization(ir, tampered)

    def test_invalid_depth_limit_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "positive integers"):
            characterize_combinational_cuts(_chain_ir(), (0,))

    def test_characterization_supports_arbitrary_positive_depth(self):
        report = characterize_combinational_cuts(_chain_ir(), (3,))
        self.assertEqual(
            report["depth_limits"][0]["max_dependency_depth"], 3
        )

    def test_cli_writes_and_independently_validates_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            report_path = root / "characterization.json"
            ir_path.write_text(
                json.dumps(_chain_ir().to_dict()), encoding="utf-8"
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "combinational-cut",
                            "characterize",
                            "--ir",
                            str(ir_path),
                            "--depth-limit",
                            "1",
                            "--depth-limit",
                            "2",
                            "--output",
                            str(report_path),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "combinational-cut",
                            "validate",
                            "--ir",
                            str(ir_path),
                            str(report_path),
                        ]
                    ),
                    0,
                )
            self.assertTrue(report_path.is_file())


class StaticExactCombinationalCutPartitionTest(unittest.TestCase):
    def setUp(self):
        self.ir = _chain_ir()
        self.platform = Platform.load(PLATFORM_PATH)
        self.constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )

    def _exact_artifacts(
        self, frame_slots=16, dependent_return=False, dependency_depth=1
    ):
        constraints = (
            normalize_partition_constraints(
                {
                    "schema": "emuflow.partition-constraints/v1",
                    "balance_tolerance": 1.0,
                },
                self.ir,
                self.platform,
            )
            if dependent_return or dependency_depth == 2
            else self.constraints
        )
        clusters = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=dependency_depth,
            comb_segment_budget_slots=1,
            frame_slots=frame_slots,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga1",
            "l2": "fpga0" if dependency_depth == 2 else "fpga1",
            "q1": (
                "fpga0"
                if dependent_return or dependency_depth == 2
                else "fpga1"
            ),
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            if (
                cluster_id in cluster_targets
                and cluster_targets[cluster_id] != target
            ):
                raise AssertionError("fixture assigns one cluster twice")
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-static-exact-v1",
            seed=0,
        )
        return clusters, assignment

    def _exact_routes(self, assignment, frame_slots=16):
        demands = demands_from_assignment(assignment, self.platform)
        constraints = normalize_route_constraints(
            None, self.platform, frame_slots=frame_slots
        )
        utilization = {
            "link_0_1:fpga0->fpga1": 0,
            "link_0_1:fpga1->fpga0": 0,
        }
        routed = []
        for demand in demands:
            source = demand["source"]
            sink = demand["sinks"][0]
            key = f"link_0_1:{source}->{sink}"
            utilization[key] += 1
            routed.append(
                {
                    **demand,
                    "tree_edges": [
                        {
                            "link": "link_0_1",
                            "from": source,
                            "to": sink,
                        }
                    ],
                    "max_latency_cycles": 2,
                }
            )
        capacity = 32 * frame_slots
        link_utilization = []
        for key in sorted(utilization):
            direction = key.split(":", 1)[1]
            used = utilization[key]
            link_utilization.append(
                {
                    "key": key,
                    "link": "link_0_1",
                    "direction": direction,
                    "capacity_bits": capacity,
                    "used_bits": used,
                    "utilization": used / capacity,
                }
            )
        routes = {
            "schema": "emuflow.system-routes/v1",
            "design": assignment["design"],
            "platform": self.platform.name,
            "provider": "native-load-balanced-multicast-v2",
            "constraints": constraints,
            "demands": demands,
            "routes": routed,
            "link_utilization": link_utilization,
            "metrics": {
                "demands": len(demands),
                "routed_sinks": len(demands),
                "tree_edges": len(demands),
                "max_link_utilization": max(
                    item["utilization"] for item in link_utilization
                ),
                "total_link_bit_hops": len(demands),
                "iterations": 1,
            },
            "semantic_contract": assignment["semantic_contract"],
            "semantic_contract_sha256": semantic_contract_sha256(
                assignment["semantic_contract"]
            ),
        }
        validate_system_routes(assignment, self.platform, routes)
        return routes

    def test_safe_default_is_identical_to_explicit_safe_mode(self):
        implicit = build_clusters(self.ir, self.constraints)
        explicit = build_clusters(
            self.ir, self.constraints, cut_mode="sequential-only"
        )
        self.assertEqual(implicit, explicit)
        self.assertNotIn("cut_mode", implicit["policy"])

    def test_exact_mode_rejects_zero_or_multiple_virtual_clocks(self):
        for clocks in (
            [],
            [
                *self.ir.value["clocks"],
                {
                    "id": "aux_clk",
                    "name": "aux_clk",
                    "source_port": "aux_clk",
                    "period_ns": None,
                },
            ],
        ):
            with self.subTest(clock_count=len(clocks)):
                value = copy.deepcopy(self.ir.value)
                value["clocks"] = clocks
                with self.assertRaisesRegex(
                    ValidationError, "exactly one virtual DUT clock"
                ):
                    build_clusters(
                        EmuIR(value),
                        self.constraints,
                        cut_mode=CUT_MODE_STATIC_EXACT,
                    )

    def test_depth_one_cut_has_independently_validated_contract(self):
        safe = build_clusters(self.ir, self.constraints)
        clusters, assignment = self._exact_artifacts()
        self.assertGreater(
            len(clusters["clusters"]), len(safe["clusters"])
        )
        combinational = [
            item
            for item in assignment["cut_nets"]
            if item["cut_class"] == "combinational"
        ]
        self.assertEqual([item["net"] for item in combinational], ["n0"])
        self.assertEqual(combinational[0]["predecessor_cut_nets"], [])
        self.assertEqual(combinational[0]["combinational_dependency_depth"], 1)
        contract = assignment["semantic_contract"]
        self.assertEqual(
            contract["qualification"],
            "partition-legality-only-provisional",
        )
        self.assertEqual(contract["metrics"]["combinational_cut_nets"], 1)
        self.assertTrue(contract["capture_requirements"])
        validation = validate_partition_artifacts(
            self.ir, self.platform, clusters, assignment
        )
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(
            validation["qualification"],
            "partition-legality-only-provisional",
        )

    def test_exact_contract_preserves_reached_memory_capture_pin(self):
        value = copy.deepcopy(self.ir.value)
        memory = next(item for item in value["instances"] if item["id"] == "q1")
        memory.update(
            {
                "type": "VTR_DP_RAM",
                "resources": {"bram": 1},
                "parameters": {"ADDR_WIDTH": 4, "DATA_WIDTH": 8},
            }
        )
        terminal = next(item for item in value["nets"] if item["id"] == "d")
        terminal["sinks"] = [
            {"instance": "q1", "port": "data1", "bit": 3}
        ]
        self.ir = EmuIR(value)
        self.constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        _clusters, assignment = self._exact_artifacts()
        captures = [
            item
            for item in assignment["semantic_contract"]["capture_requirements"]
            if item["kind"] == "architectural-state"
            and item["endpoint"] == "q1"
        ]
        self.assertEqual(len(captures), 1)
        self.assertEqual(
            captures,
            [
                {
                    "id": "capture000000",
                    "cut_net": "n0",
                    "fpga": "fpga1",
                    "kind": "architectural-state",
                    "endpoint": "q1",
                    "port": "data1",
                    "bit": 3,
                }
            ],
        )

    def test_contract_tamper_is_rejected(self):
        clusters, assignment = self._exact_artifacts()
        tampered = copy.deepcopy(assignment)
        tampered["semantic_contract"]["commit_slot"] -= 1
        with self.assertRaisesRegex(ValidationError, "semantic_contract"):
            validate_partition_artifacts(
                self.ir, self.platform, clusters, tampered
            )

    def test_exact_cluster_policy_tamper_is_rejected(self):
        clusters, assignment = self._exact_artifacts()
        tampered = copy.deepcopy(clusters)
        tampered["policy"]["eligible_combinational_cut_nets"].append("n1")
        with self.assertRaisesRegex(ValidationError, "reconstruction"):
            validate_partition_artifacts(
                self.ir, self.platform, tampered, assignment
            )

    def test_depth_two_contract_schedule_and_macro_cycle_are_exact(self):
        clusters, assignment = self._exact_artifacts(dependency_depth=2)
        combinational = [
            item
            for item in assignment["cut_nets"]
            if item["cut_class"] == "combinational"
        ]
        self.assertEqual([item["net"] for item in combinational], ["n0", "n1"])
        self.assertEqual(combinational[1]["predecessor_cut_nets"], ["n0"])
        self.assertEqual(
            assignment["semantic_contract"]["metrics"][
                "maximum_combinational_dependency_depth"
            ],
            2,
        )
        self.assertEqual(
            validate_partition_artifacts(
                self.ir, self.platform, clusters, assignment
            )["status"],
            "pass",
        )
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )
        readiness = {
            item["net"]: item
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        self.assertEqual(readiness["n1"]["evidence"][0]["predecessor_cut_net"], "n0")
        self.assertGreater(
            readiness["n1"]["source_ready_slot"],
            readiness["n0"]["source_ready_slot"],
        )
        split = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        self.assertEqual(
            validate_split_artifacts(
                self.ir,
                assignment,
                schedule,
                self.platform,
                split,
            )["status"],
            "pass",
        )
        random_evidence = simulate_static_exact_partition_equivalence(
            self.ir, assignment, schedule, cycles=8, seed=23
        )
        self.assertEqual(random_evidence["status"], "pass")
        exhaustive = exhaustively_verify_static_exact_partition_equivalence(
            self.ir, assignment, schedule
        )
        self.assertEqual(exhaustive["status"], "pass")

    def test_depth_above_two_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "1 or 2"):
            build_clusters(
                self.ir,
                self.constraints,
                cut_mode=CUT_MODE_STATIC_EXACT,
                max_cross_fpga_dependency_depth=3,
            )

    def test_v2_releases_deep_potential_candidate_with_actual_depth_one(self):
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        legacy = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
        )
        generalized = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
            comb_segment_budget_slots=1,
            frame_slots=16,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        self.assertEqual(
            legacy["policy"]["eligible_combinational_cut_nets"], ["n0"]
        )
        self.assertEqual(
            generalized["policy"]["eligible_combinational_cut_nets"],
            ["n0", "n1"],
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in generalized["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga0",
            "l2": "fpga1",
            "q1": "fpga1",
        }
        cluster_targets = {}
        for instance, target in targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            generalized,
            constraints,
            cluster_targets,
            provider="test-static-exact-v2",
            seed=0,
        )
        contract = assignment["semantic_contract"]
        self.assertEqual(
            contract["schema"],
            GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA,
        )
        self.assertEqual(contract["metrics"]["combinational_cut_nets"], 1)
        self.assertEqual(
            contract["metrics"]["maximum_combinational_dependency_depth"],
            1,
        )
        cut = next(
            item for item in assignment["cut_nets"] if item["net"] == "n1"
        )
        self.assertEqual(cut["predecessor_cut_nets"], [])
        self.assertEqual(cut["combinational_dependency_depth"], 1)
        self.assertEqual(
            validate_partition_artifacts(
                self.ir, self.platform, generalized, assignment
            )["status"],
            "pass",
        )
        tampered = copy.deepcopy(assignment)
        del tampered["semantic_contract"][
            "uncongested_schedule_lower_bound"
        ]
        with self.assertRaisesRegex(ValidationError, "certificate is incomplete"):
            demands_from_assignment(tampered, self.platform)
        routes = self._exact_routes(assignment)
        tampered_routes = copy.deepcopy(routes)
        del tampered_routes["semantic_contract"][
            "uncongested_schedule_lower_bound"
        ]
        tampered_routes["semantic_contract_sha256"] = semantic_contract_sha256(
            tampered_routes["semantic_contract"]
        )
        with self.assertRaisesRegex(ValidationError, "certificate is incomplete"):
            build_tdm_schedule(tampered_routes, self.platform)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_v2_depth_three_runs_through_schedule_split_and_equivalence(self):
        value = copy.deepcopy(self.ir.value)
        value["instances"].insert(
            -1,
            {
                "id": "l3",
                "type": "LUT2",
                "parameters": {"INIT": "1010"},
                "resources": {"lut": 1},
            },
        )
        terminal = next(item for item in value["nets"] if item["id"] == "d")
        terminal["drivers"] = [_endpoint("l3", "O")]
        value["nets"].insert(
            -1,
            {
                "id": "n2",
                "name": "n2",
                "cut_class": "combinational",
                "drivers": [_endpoint("l2", "O")],
                "sinks": [_endpoint("l3", "I0")],
            },
        )
        self.ir = EmuIR(value)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            self.platform,
        )
        clusters = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=3,
            comb_segment_budget_slots=1,
            frame_slots=24,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga1",
            "l2": "fpga0",
            "l3": "fpga1",
            "q1": "fpga1",
        }
        cluster_targets = {}
        for instance, target in targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-static-exact-v2",
            seed=0,
        )
        contract = assignment["semantic_contract"]
        self.assertEqual(
            contract["metrics"]["maximum_combinational_dependency_depth"],
            3,
        )
        self.assertEqual(
            [
                item["net"]
                for item in assignment["cut_nets"]
                if item["cut_class"] == "combinational"
            ],
            ["n0", "n1", "n2"],
        )
        lower_bound = contract["uncongested_schedule_lower_bound"]
        self.assertEqual(
            lower_bound["provider"],
            "board-minimum-latency-dag-lower-bound-v1",
        )
        self.assertGreaterEqual(
            lower_bound["minimum_capture_slack_slots"], 0
        )
        with self.assertRaisesRegex(
            ValidationError, "uncongested minimum-latency"
        ):
            build_static_exact_semantic_contract(
                self.ir,
                self.platform.to_dict(),
                assignment["instance_assignment"],
                assignment["cut_nets"],
                max_dependency_depth=3,
                comb_segment_budget_slots=1,
                frame_slots=8,
                candidate_selection_policy=(
                    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
                ),
            )
        routes = self._exact_routes(assignment, frame_slots=24)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )
        self.assertEqual(
            schedule["schedule_dependency_certificate"][
                "topological_cut_order"
            ],
            ["n0", "n1", "n2"],
        )
        split = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        self.assertEqual(
            validate_split_artifacts(
                self.ir,
                assignment,
                schedule,
                self.platform,
                split,
            )["status"],
            "pass",
        )
        self.assertEqual(
            exhaustively_verify_static_exact_partition_equivalence(
                self.ir, assignment, schedule
            )["status"],
            "pass",
        )

    def test_reconvergent_depth_two_waits_for_every_predecessor(self):
        ir = _reconvergent_ir()
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            ir,
            self.platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=2,
            comb_segment_budget_slots=1,
            frame_slots=16,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "qa": "fpga0",
            "qb": "fpga0",
            "qc": "fpga1",
            "la": "fpga0",
            "lb": "fpga0",
            "merge": "fpga1",
            "after": "fpga0",
            "qout": "fpga0",
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            self.assertNotIn(cluster_id, cluster_targets)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            ir,
            self.platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-reconvergent-static-exact-v1",
            seed=0,
        )
        node_by_net = {
            item["net"]: item
            for item in assignment["semantic_contract"]["cut_nodes"]
        }
        self.assertEqual(node_by_net["c"]["predecessor_cut_nets"], ["a", "b"])
        segment_by_id = {
            item["id"]: item
            for item in assignment["semantic_contract"]["logic_segments"]
        }
        source_segments = [
            segment_by_id[item]
            for item in node_by_net["c"]["source_segment_ids"]
        ]
        self.assertEqual(
            [item["kind"] for item in source_segments],
            ["launch_to_tx", "rx_to_tx", "rx_to_tx"],
        )
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        readiness = {
            item["net"]: item
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        evidence = readiness["c"]["evidence"]
        self.assertEqual(
            [
                item["predecessor_cut_net"]
                for item in evidence
                if item["kind"] == "rx_to_tx"
            ],
            ["a", "b"],
        )
        self.assertEqual(
            [item["kind"] for item in evidence],
            ["launch_to_tx", "rx_to_tx", "rx_to_tx"],
        )
        self.assertEqual(
            readiness["c"]["source_ready_slot"],
            max(item["ready_slot"] for item in evidence),
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_one_logical_cut_can_use_two_route_hops_without_depth_inflation(self):
        platform = _three_fpga_platform("line")
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            self.ir,
            platform,
        )
        clusters = build_clusters(
            self.ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
            comb_segment_budget_slots=1,
            frame_slots=16,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            cluster_for["q0"]: "fpga0",
            cluster_for["l0"]: "fpga0",
            cluster_for["l1"]: "fpga2",
            cluster_for["q1"]: "fpga2",
        }
        assignment = build_partition_assignment(
            self.ir,
            platform,
            clusters,
            constraints,
            targets,
            provider="test-multihop-static-exact-v1",
            seed=0,
        )
        route_constraints = normalize_route_constraints(
            None, platform, frame_slots=16
        )
        routes = route_system_native(
            assignment, platform, route_constraints
        )
        self.assertEqual(len(routes["routes"]), 1)
        self.assertEqual(len(routes["routes"][0]["tree_edges"]), 2)
        self.assertEqual(
            assignment["semantic_contract"]["metrics"][
                "maximum_combinational_dependency_depth"
            ],
            1,
        )
        schedule = build_tdm_schedule(routes, platform)
        entries = sorted(
            schedule["entries"], key=lambda item: item["hop"]
        )
        self.assertEqual([item["hop"] for item in entries], [0, 1])
        self.assertGreaterEqual(
            entries[1]["slot"], entries[0]["arrival_slot"] + 1
        )
        self.assertEqual(
            validate_tdm_schedule(routes, platform, schedule)["status"],
            "pass",
        )

    def test_multicast_cut_checks_every_sink_arrival_and_capture(self):
        ir = _wide_fanout_ir(width=2)
        platform = _three_fpga_platform("star")
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
            comb_segment_budget_slots=1,
            frame_slots=16,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "q0": "fpga0",
            "source_lut": "fpga0",
            "sink_lut_000": "fpga1",
            "sink_ff_000": "fpga1",
            "sink_lut_001": "fpga2",
            "sink_ff_001": "fpga2",
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-multicast-static-exact-v1",
            seed=0,
        )
        route_constraints = normalize_route_constraints(
            None, platform, frame_slots=16
        )
        routes = route_system_native(
            assignment, platform, route_constraints
        )
        self.assertEqual(len(routes["routes"]), 1)
        self.assertEqual(routes["routes"][0]["sinks"], ["fpga1", "fpga2"])
        self.assertEqual(len(routes["routes"][0]["tree_edges"]), 2)
        schedule = build_tdm_schedule(routes, platform)
        completion = schedule["demand_completions"][0]
        self.assertEqual(
            set(completion["sink_arrival_slots"]), {"fpga1", "fpga2"}
        )
        capture_records = schedule["schedule_dependency_certificate"][
            "capture_readiness"
        ]
        self.assertEqual(
            {item["fpga"] for item in capture_records}, {"fpga1", "fpga2"}
        )
        self.assertEqual(
            validate_tdm_schedule(routes, platform, schedule)["status"],
            "pass",
        )

    def test_unrelated_slow_level_one_demand_does_not_gate_ready_chain(self):
        value = self.ir.to_dict()
        value["design"]["name"] = "path_local_readiness"
        value["instances"].extend(
            [
                {"id": "qu", "type": "FDRE", "resources": {"ff": 1}},
                {"id": "lu0", "type": "LUT2", "resources": {"lut": 1}},
                {"id": "lu1", "type": "LUT2", "resources": {"lut": 1}},
                {"id": "quout", "type": "FDRE", "resources": {"ff": 1}},
            ]
        )
        value["nets"].extend(
            [
                {
                    "id": "qu_q",
                    "name": "qu_q",
                    "cut_class": "register_output",
                    "drivers": [_endpoint("qu", "Q")],
                    "sinks": [_endpoint("lu0", "I0")],
                },
                {
                    "id": "u",
                    "name": "u",
                    "cut_class": "combinational",
                    "drivers": [_endpoint("lu0", "O")],
                    "sinks": [_endpoint("lu1", "I0")],
                },
                {
                    "id": "u_d",
                    "name": "u_d",
                    "cut_class": "register_input",
                    "drivers": [_endpoint("lu1", "O")],
                    "sinks": [_endpoint("quout", "D")],
                },
            ]
        )
        ir = EmuIR(value)
        platform = _three_fpga_platform("line")
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
            },
            ir,
            platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=2,
            comb_segment_budget_slots=1,
            frame_slots=16,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        instance_targets = {
            "q0": "fpga0",
            "l0": "fpga0",
            "l1": "fpga1",
            "l2": "fpga0",
            "q1": "fpga0",
            "qu": "fpga0",
            "lu0": "fpga0",
            "lu1": "fpga2",
            "quout": "fpga2",
        }
        cluster_targets = {}
        for instance, target in instance_targets.items():
            cluster_id = cluster_for[instance]
            if cluster_id in cluster_targets:
                self.assertEqual(cluster_targets[cluster_id], target)
            cluster_targets[cluster_id] = target
        assignment = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            cluster_targets,
            provider="test-path-local-static-exact-v1",
            seed=0,
        )
        routes = route_system_native(
            assignment,
            platform,
            normalize_route_constraints(None, platform, frame_slots=16),
        )
        schedule = build_tdm_schedule(routes, platform)
        readiness = {
            item["net"]: item["source_ready_slot"]
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        completions = {
            item["net"]: item["completion_slot"]
            for item in schedule["demand_completions"]
        }
        self.assertLess(readiness["n1"], completions["u"])
        self.assertEqual(
            validate_tdm_schedule(routes, platform, schedule)["status"],
            "pass",
        )

    def test_wide_cone_splits_and_improves_checked_balance(self):
        ir = _wide_fanout_ir()
        constraints = normalize_partition_constraints(None, ir, self.platform)
        safe_clusters = build_clusters(ir, constraints)
        exact_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            frame_slots=16,
        )
        self.assertEqual(
            max(len(item["instances"]) for item in safe_clusters["clusters"]),
            33,
        )
        self.assertEqual(
            max(len(item["instances"]) for item in exact_clusters["clusters"]),
            1,
        )
        safe_assignment = assign_clusters(
            ir, self.platform, safe_clusters, constraints, seed=9
        )
        exact_assignment = assign_clusters(
            ir, self.platform, exact_clusters, constraints, seed=9
        )
        safe_validation = validate_partition_artifacts(
            ir, self.platform, safe_clusters, safe_assignment
        )
        exact_validation = validate_partition_artifacts(
            ir, self.platform, exact_clusters, exact_assignment
        )
        self.assertLess(
            exact_validation["effective_balance_percent"],
            safe_validation["effective_balance_percent"],
        )
        self.assertGreater(
            exact_validation["combinational_cut_nets"], 0
        )

    def test_phase3_cli_emits_opt_in_provisional_qualification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            output = root / "phase3"
            ir_path.write_text(
                json.dumps(self.ir.to_dict()), encoding="utf-8"
            )
            with redirect_stdout(io.StringIO()):
                status = main(
                    [
                        "phase3",
                        "--ir",
                        str(ir_path),
                        "--platform",
                        str(PLATFORM_PATH),
                        "--provider",
                        "greedy",
                        "--cut-mode",
                        "static-exact-combinational",
                        "--max-cross-fpga-dependency-depth",
                        "1",
                        "--out",
                        str(output),
                    ]
                )
            self.assertEqual(status, 0)
            report = json.loads(
                (output / "phase3_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["qualification"],
                "partition-legality-only-provisional",
            )
            self.assertEqual(
                report["validation"]["cut_mode"],
                "static-exact-combinational",
            )

    def test_phase4_propagates_exact_contract_without_mutation(self):
        _, assignment = self._exact_artifacts()
        routes = self._exact_routes(assignment)
        validation = validate_system_routes(assignment, self.platform, routes)
        self.assertEqual(validation["status"], "pass")
        tampered = copy.deepcopy(routes)
        tampered["semantic_contract"]["commit_slot"] -= 1
        with self.assertRaisesRegex(ValidationError, "semantic_contract"):
            validate_system_routes(assignment, self.platform, tampered)

    def test_phase5_uses_path_local_predecessor_readiness(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        self.assertEqual(schedule["provider"], TDM_STATIC_EXACT_PROVIDER)
        self.assertEqual(
            schedule["schedule_dependency_certificate"]["schema"],
            TDM_STATIC_EXACT_CERTIFICATE_SCHEMA,
        )
        validation = validate_tdm_schedule(
            routes, self.platform, schedule
        )
        self.assertEqual(
            validation["qualification"],
            "dependency-schedule-readiness-pass",
        )
        readiness = {
            item["net"]: item
            for item in schedule["schedule_dependency_certificate"][
                "demand_readiness"
            ]
        }
        self.assertEqual(readiness["n0"]["source_ready_slot"], 1)
        self.assertEqual(readiness["d"]["source_ready_slot"], 4)
        self.assertEqual(
            readiness["d"]["evidence"][0]["predecessor_cut_net"],
            "n0",
        )
        tampered = copy.deepcopy(schedule)
        entry = next(item for item in tampered["entries"] if item["net"] == "d")
        entry["ready_slot"] -= 1
        with self.assertRaisesRegex(ValidationError, "inconsistent"):
            validate_tdm_schedule(routes, self.platform, tampered)

        tampered = copy.deepcopy(schedule)
        tampered["entries"][0]["arrival_slot"] += 1
        with self.assertRaisesRegex(ValidationError, "inconsistent"):
            validate_tdm_schedule(routes, self.platform, tampered)

        tampered = copy.deepcopy(schedule)
        tampered["schedule_dependency_certificate"]["capture_readiness"][0][
            "ready_slot"
        ] += 1
        with self.assertRaisesRegex(ValidationError, "certificate"):
            validate_tdm_schedule(routes, self.platform, tampered)

    def test_register_output_launch_reserves_configured_settle_budget(self):
        """A cross-FPGA FF output cannot be sampled by TX in slot zero."""
        clusters = build_clusters(
            self.ir,
            self.constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=1,
            comb_segment_budget_slots=1,
            frame_slots=16,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        targets = {
            cluster_for["q0"]: "fpga0",
            cluster_for["l0"]: "fpga1",
            cluster_for["l1"]: "fpga1",
            cluster_for["l2"]: "fpga1",
            cluster_for["q1"]: "fpga1",
        }
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            self.constraints,
            targets,
            provider="test-static-exact-register-launch-v1",
            seed=0,
        )
        node = next(
            item
            for item in assignment["semantic_contract"]["cut_nodes"]
            if item["net"] == "q"
        )
        segments = {
            item["id"]: item
            for item in assignment["semantic_contract"]["logic_segments"]
        }
        launch = segments[node["source_segment_ids"][0]]
        self.assertEqual(launch["kind"], "launch_to_tx")
        self.assertEqual(launch["budget_slots"], 1)

        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        readiness = next(
            item
            for item in schedule["schedule_dependency_certificate"]
            ["demand_readiness"]
            if item["net"] == "q"
        )
        self.assertEqual(readiness["source_ready_slot"], 1)
        self.assertGreaterEqual(
            min(item["slot"] for item in schedule["entries"] if item["net"] == "q"),
            1,
        )
        self.assertEqual(
            validate_tdm_schedule(routes, self.platform, schedule)["status"],
            "pass",
        )

    def test_dependency_free_constant_cone_is_stable_at_slot_zero(self):
        ir = EmuIR(
            {
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "constant_cut",
                    "top": "constant_cut",
                    "source_format": "test",
                },
                "ports": [],
                "instances": [
                    {
                        "id": "constant_lut",
                        "type": "LUT2",
                        "parameters": {"INIT": "1111"},
                        "resources": {"lut": 1},
                    },
                    {
                        "id": "sink_lut",
                        "type": "LUT2",
                        "parameters": {"INIT": "1010"},
                        "resources": {"lut": 1},
                    },
                    {"id": "sink_ff", "type": "FDRE", "resources": {"ff": 1}},
                ],
                "nets": [
                    {
                        "id": "constant_cross",
                        "name": "constant_cross",
                        "cut_class": "combinational",
                        "drivers": [_endpoint("constant_lut", "O")],
                        "sinks": [_endpoint("sink_lut", "I0")],
                    },
                    {
                        "id": "d",
                        "name": "d",
                        "cut_class": "register_input",
                        "drivers": [_endpoint("sink_lut", "O")],
                        "sinks": [_endpoint("sink_ff", "D")],
                    },
                ],
                "clocks": [
                    {
                        "id": "clk",
                        "name": "clk",
                        "source_port": "clk",
                        "period_ns": None,
                    }
                ],
                "warnings": [],
            }
        )
        contract = build_static_exact_semantic_contract(
            ir,
            {"name": "test-platform"},
            {
                "constant_lut": "fpga0",
                "sink_lut": "fpga1",
                "sink_ff": "fpga1",
            },
            [
                {
                    "net": "constant_cross",
                    "source_fpgas": ["fpga0"],
                    "sink_fpgas": ["fpga1"],
                }
            ],
            max_dependency_depth=1,
            comb_segment_budget_slots=1,
            frame_slots=16,
        )
        launch = next(
            item
            for item in contract["logic_segments"]
            if item["kind"] == "launch_to_tx"
        )
        self.assertEqual(launch["budget_slots"], 0)
        self.assertEqual(
            launch["source_semantics"], "configuration-stable-constant"
        )
        self.assertEqual(
            launch["evidence"],
            "structurally-proven-configuration-stable-constant",
        )

    def test_phase5_cli_writes_and_revalidates_exact_schedule(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            routes_path = root / "routes.json"
            output = root / "phase5"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "phase5",
                            "--routes",
                            str(routes_path),
                            "--platform",
                            str(PLATFORM_PATH),
                            "--out",
                            str(output),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "schedule",
                            "validate",
                            str(output / "schedule.json"),
                            "--routes",
                            str(routes_path),
                            "--platform",
                            str(PLATFORM_PATH),
                        ]
                    ),
                    0,
                )
            report = json.loads(
                (output / "phase5_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["qualification"],
                "dependency-schedule-readiness-pass",
            )
            self.assertEqual(report["cut_mode"], "static-exact-combinational")

    def test_phase4_rejects_exact_contract_digest_tamper(self):
        _, assignment = self._exact_artifacts()
        routes = self._exact_routes(assignment)
        tampered = copy.deepcopy(routes)
        tampered["semantic_contract_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "semantic_contract"):
            validate_system_routes(assignment, self.platform, tampered)

    def test_phase5_rejects_fixed_frame_that_cannot_meet_capture(self):
        _, assignment = self._exact_artifacts(
            frame_slots=4, dependent_return=True
        )
        routes = self._exact_routes(assignment, frame_slots=4)
        with self.assertRaisesRegex(ValidationError, "infeasible"):
            build_tdm_schedule(routes, self.platform)

    def test_phase6_event_model_uses_local_slot_values_and_is_equivalent(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        random_evidence = simulate_static_exact_partition_equivalence(
            self.ir,
            assignment,
            schedule,
            cycles=8,
            seed=19,
        )
        self.assertEqual(random_evidence["status"], "pass")
        self.assertEqual(
            random_evidence["evidence_type"], "random-simulation"
        )
        self.assertEqual(
            random_evidence["startup_uninitialized_shadow_reads"], 0
        )
        self.assertEqual(random_evidence["source_full_evaluations"], 0)
        self.assertEqual(random_evidence["reference_full_evaluations"], 8)
        self.assertEqual(
            random_evidence["initialization_full_evaluations"], 0
        )
        self.assertEqual(random_evidence["partition_full_evaluations"], 0)
        self.assertGreaterEqual(
            random_evidence["incremental_combinational_cell_evaluations"], 0
        )
        exhaustive = exhaustively_verify_static_exact_partition_equivalence(
            self.ir,
            assignment,
            schedule,
        )
        self.assertEqual(exhaustive["status"], "pass")
        self.assertEqual(exhaustive["evidence_type"], "exhaustive-small-model")
        self.assertEqual(exhaustive["cases"], 4)
        self.assertEqual(exhaustive["full_replay_cross_checks"], 4)
        self.assertEqual(exhaustive["source_full_evaluations"], 0)
        self.assertEqual(exhaustive["reference_full_evaluations"], 4)
        self.assertEqual(exhaustive["initialization_full_evaluations"], 0)
        self.assertEqual(exhaustive["partition_full_evaluations"], 0)

    def test_phase6_event_model_rejects_tx_before_local_source_ready(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        routes = self._exact_routes(assignment)
        schedule = build_tdm_schedule(routes, self.platform)
        tampered = copy.deepcopy(schedule)
        first = next(item for item in tampered["entries"] if item["net"] == "n0")
        first["slot"] = 0
        first["ready_slot"] = 0
        with self.assertRaisesRegex(ValidationError, "before source-ready"):
            simulate_static_exact_partition_equivalence(
                self.ir,
                assignment,
                tampered,
                cycles=1,
            )

    def test_phase6_materializes_preserved_exact_boundaries_without_bypass(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        schedule = build_tdm_schedule(
            self._exact_routes(assignment), self.platform
        )
        artifacts = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        validation = validate_split_artifacts(
            self.ir,
            assignment,
            schedule,
            self.platform,
            artifacts,
        )
        self.assertEqual(validation["cut_mode"], "static-exact-combinational")
        self.assertEqual(validation["hidden_cross_fpga_bypass_errors"], 0)
        self.assertEqual(
            validation["exact_boundary_identities"],
            2 * len(schedule["entries"]),
        )
        boundaries = [
            endpoint["boundary_identity"]
            for transport in artifacts["transports"].values()
            for endpoint in transport["endpoints"]
        ]
        self.assertEqual(len(boundaries), len(set(boundaries)))
        for fpga_id, transport in artifacts["transports"].items():
            rtl = transport_to_systemverilog(transport, self.platform)
            self.assertIn('(* KEEP = "yes", DONT_TOUCH = "yes" *)', rtl)
            self.assertIn(fpga_id, artifacts["netlists"])

        tampered = copy.deepcopy(artifacts)
        shadow = next(
            segment
            for netlist in tampered["netlists"].values()
            for segment in netlist["nets"]
            if segment["source_kind"] == "transport_shadow"
        )
        shadow["drivers"] = [_endpoint("l0", "O")]
        with self.assertRaises(ValidationError):
            validate_split_artifacts(
                self.ir,
                assignment,
                schedule,
                self.platform,
                tampered,
            )

    def test_phase6_run_and_independent_validate_use_exact_macro_model(self):
        _, assignment = self._exact_artifacts(dependent_return=True)
        schedule = build_tdm_schedule(
            self._exact_routes(assignment), self.platform
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.json"
            assignment_path = root / "assignment.json"
            schedule_path = root / "schedule.json"
            output = root / "phase6"
            ir_path.write_text(json.dumps(self.ir.to_dict()), encoding="utf-8")
            assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            report = run_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output,
                equivalence_cycles=4,
                equivalence_seed=31,
            )
            evidence = report["equivalence"]
            self.assertEqual(evidence["status"], "pass")
            self.assertEqual(evidence["random_trace_count"], 3)
            self.assertEqual(evidence["random_macro_cycles"], 12)
            self.assertEqual(evidence["source_full_evaluations"], 0)
            self.assertEqual(evidence["reference_full_evaluations"], 12)
            self.assertEqual(evidence["initialization_full_evaluations"], 0)
            self.assertEqual(evidence["partition_full_evaluations"], 0)
            self.assertEqual(
                evidence["exhaustive_macro_step"]["evidence_type"],
                "exhaustive-small-model",
            )
            validation = validate_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output / "manifest.json",
            )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(
                validation["static_exact_equivalence"]["status"], "pass"
            )
            with patch(
                "emuflow.phase6._static_exact_equivalence_evidence",
                side_effect=AssertionError("equivalence replay was not reusable"),
            ):
                structural_validation = validate_phase6(
                    ir_path,
                    assignment_path,
                    schedule_path,
                    PLATFORM_PATH,
                    output / "manifest.json",
                    replay_equivalence=False,
                )
            self.assertEqual(structural_validation["status"], "pass")
            self.assertNotIn(
                "static_exact_equivalence", structural_validation
            )

    @unittest.skipUnless(shutil.which("yosys"), "yosys is not installed")
    def test_phase6_canonical_macro_step_formal_miter(self):
        fixture = (
            ROOT
            / "tests"
            / "fixtures"
            / "static_exact_macro_step_miter.sv"
        )
        command = (
            f"read_verilog -formal -sv {fixture}; "
            "prep -top static_exact_macro_step_miter; "
            "chformal -lower; "
            "sat -verify -prove-asserts"
        )
        completed = subprocess.run(
            [shutil.which("yosys"), "-q", "-p", command],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
