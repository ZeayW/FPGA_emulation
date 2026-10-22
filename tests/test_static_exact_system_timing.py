import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.board_link_timing import build_board_link_timing_model
from emuflow.cross_layer_timing import (
    build_cross_layer_physical_binding,
    build_cross_layer_timing_contract,
)
from emuflow.errors import ValidationError
from emuflow.combinational_cut import semantic_contract_sha256
from emuflow.phase5 import validate_phase5
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from emuflow.static_exact_timing import (
    build_static_exact_segment_deadlines,
    validate_static_exact_segment_deadlines,
)
from emuflow.runtime import build_virtual_runtime
from emuflow.system_timing import (
    _fixed_slot_transport_path_delay,
    build_system_timing,
)
from emuflow.tdm import (
    build_tdm_schedule,
    reconstruct_tdm_schedule_timing,
    validate_tdm_schedule,
)
from emuflow.tdm_schedule_storage import (
    ScheduleRecordSequence,
    read_tdm_schedule,
    write_tdm_schedule_storage,
)
from tests import test_combinational_cut as combinational_fixture


class StaticExactSystemTimingTest(unittest.TestCase):
    def setUp(self):
        fixture = (
            combinational_fixture.StaticExactCombinationalCutPartitionTest()
        )
        fixture.setUp()
        _, self.assignment = fixture._exact_artifacts(dependent_return=True)
        self.platform = fixture.platform
        hydrated_routes = fixture._exact_routes(self.assignment)
        self.schedule = build_tdm_schedule(
            hydrated_routes, self.platform
        )
        self.routes = fixture._persisted_routes(hydrated_routes)
        digest = self.schedule["semantic_contract_sha256"]
        entry_by_net = {item["net"]: item for item in self.schedule["entries"]}
        records = {
            "fpga0": [
                {
                    "id": "physical-launch",
                    "kind": "launch",
                    "system_path": "path0",
                    "member_path": "path0",
                    "cut_index": 0,
                    "fpga": "fpga0",
                    "replace_tx_endpoint": f"__emuflow_tx_{entry_by_net['n0']['id']}",
                    "start_pin": "q0.Q",
                    "end_pin": "tx_n0",
                    "static_exact_segment_id": "segment000000",
                    "delay_ns": 3.0,
                    "measurement": "endpoint-exact",
                },
                {
                    "id": "physical-capture",
                    "kind": "capture",
                    "system_path": "path0",
                    "member_path": "path0",
                    "cut_index": 2,
                    "fpga": "fpga0",
                    "replace_tx_endpoint": None,
                    "start_pin": "rx_d.Q",
                    "end_pin": "q1.D",
                    "static_exact_segment_id": "segment000002",
                    "delay_ns": 7.0,
                    "measurement": "endpoint-exact",
                },
            ],
            "fpga1": [
                {
                    "id": "physical-transition",
                    "kind": "transition",
                    "system_path": "path0",
                    "member_path": "path0",
                    "cut_index": 1,
                    "fpga": "fpga1",
                    "replace_tx_endpoint": f"__emuflow_tx_{entry_by_net['d']['id']}",
                    "start_pin": "rx_n0.Q",
                    "end_pin": "tx_d",
                    "static_exact_segment_id": "segment000001",
                    "delay_ns": 2.5,
                    "measurement": "endpoint-exact",
                }
            ],
        }
        timing = {}
        for fpga, segments in records.items():
            timing[fpga] = {
                "schema": "emuflow.logic-segment-timing/v1",
                "status": "pass",
                "design": self.schedule["design"],
                "platform": self.platform.name,
                "fpga": fpga,
                "provider": "test-routed-segment-provider",
                "qualification": "routed-test-evidence",
                "coverage": {
                    "segments": len(segments),
                    "system_paths": 1,
                    "member_paths": 1,
                    "unsupported_member_paths": 0,
                },
                "unsupported_member_paths": [],
                "segments": segments,
                "semantic_contract_sha256": digest,
            }
        self.physical = {"logic_segment_timing": timing}

    def _system_physical(self):
        result = copy.deepcopy(self.physical)
        identities = {}
        timing = {}
        for fpga in ("fpga0", "fpga1"):
            endpoint_records = []
            timing_records = []
            for entry in self.schedule["entries"]:
                for kind, endpoint_fpga in (
                    ("tx", entry["from"]),
                    ("rx", entry["to"]),
                ):
                    if endpoint_fpga != fpga:
                        continue
                    endpoint_id = f"__emuflow_{kind}_{entry['id']}"
                    endpoint_records.append(
                        {
                            "id": endpoint_id,
                            "kind": kind,
                            "schedule_entry": entry["id"],
                        }
                    )
                    timing_records.append(
                        {
                            "id": endpoint_id,
                            "kind": kind,
                            "schedule_entry": entry["id"],
                            "delay_ns": 0.5,
                            "start_object": "start",
                            "end_object": "end",
                            "measurement": (
                                "logical-source-to-tx-port"
                                if kind == "tx"
                                else "rx-port-to-shadow-capture"
                            ),
                        }
                    )
            coverage = {
                "endpoints": len(endpoint_records),
                "tx": sum(item["kind"] == "tx" for item in endpoint_records),
                "rx": sum(item["kind"] == "rx" for item in endpoint_records),
            }
            identities[fpga] = {
                "schema": "emuflow.boundary-identity/v1",
                "status": "pass",
                "design": self.schedule["design"],
                "platform": self.platform.name,
                "fpga": fpga,
                "provider": "test-boundary-identity",
                "coverage": {
                    **coverage,
                    "external_port_nets": len(endpoint_records),
                },
                "endpoints": endpoint_records,
            }
            timing[fpga] = {
                "schema": "emuflow.boundary-timing/v1",
                "status": "pass",
                "design": self.schedule["design"],
                "platform": self.platform.name,
                "fpga": fpga,
                "provider": "test-routed-boundary-provider",
                "qualification": "endpoint-exact",
                "coverage": coverage,
                "endpoints": timing_records,
            }
        result.update(
            {
                "provider": "test-physical",
                "qualification": "routed-test-evidence",
                "boundary_identities": identities,
                "boundary_timing": timing,
                "fpgas": [
                    {
                        "fpga": fpga,
                        "clock_domain_delays_ns": {
                            "dut": 8.0,
                            "cross": 2.0,
                        },
                    }
                    for fpga in ("fpga0", "fpga1")
                ],
            }
        )
        return result

    def test_indexed_contract_schedules_from_routed_dependency_dag(self):
        fixture = (
            combinational_fixture.StaticExactCombinationalCutPartitionTest()
        )
        fixture.setUp()
        _, assignment = fixture._exact_artifacts(dependent_return=True)
        routes = fixture._exact_routes(assignment)
        routes["semantic_contract"] = {
            "schema": assignment["semantic_contract"]["schema"],
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        schedule = build_tdm_schedule(routes, fixture.platform)
        self.assertEqual(
            schedule["schedule_dependency_certificate"]["schema"],
            "emuflow.sampled-virtual-wire-schedule-certificate/v2",
        )
        self.assertEqual(
            schedule["metrics"]["dependency_edges"],
            sum(
                len(route["predecessor_cut_nets"])
                for route in routes["routes"]
            ),
        )
        self.assertEqual(
            validate_tdm_schedule(routes, fixture.platform, schedule)[
                "status"
            ],
            "pass",
        )

    def test_indexed_schedule_validation_matches_legacy_without_hop_decode(self):
        fixture = (
            combinational_fixture.StaticExactCombinationalCutPartitionTest()
        )
        fixture.setUp()
        _, assignment = fixture._exact_artifacts(dependent_return=True)
        routes = fixture._exact_routes(assignment)
        routes["semantic_contract"] = {
            "schema": assignment["semantic_contract"]["schema"],
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        schedule = build_tdm_schedule(routes, fixture.platform)
        legacy = validate_tdm_schedule(routes, fixture.platform, schedule)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "schedule.json"
            write_tdm_schedule_storage(path, schedule)
            indexed = read_tdm_schedule(path)
            with mock.patch.object(
                ScheduleRecordSequence,
                "__iter__",
                side_effect=AssertionError("hop records were decoded"),
            ):
                self.assertEqual(
                    validate_tdm_schedule(routes, fixture.platform, indexed),
                    legacy,
                )

    def test_indexed_schedule_validation_rejects_tamper_and_missing_hop(self):
        fixture = (
            combinational_fixture.StaticExactCombinationalCutPartitionTest()
        )
        fixture.setUp()
        _, assignment = fixture._exact_artifacts(dependent_return=True)
        routes = fixture._exact_routes(assignment)
        routes["semantic_contract"] = {
            "schema": assignment["semantic_contract"]["schema"],
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        schedule = build_tdm_schedule(routes, fixture.platform)
        cases = []
        tampered = copy.deepcopy(schedule)
        tampered["entries"][0]["ready_slot"] += 1
        cases.append((tampered, "ready-slot mismatch"))
        missing = copy.deepcopy(schedule)
        missing["entries"].pop()
        cases.append((missing, "route-hop coverage"))
        collision = copy.deepcopy(schedule)
        duplicate = copy.deepcopy(collision["entries"][0])
        duplicate["id"] = "s999999"
        duplicate["demand"] = "unexpected-demand"
        collision["entries"].append(duplicate)
        cases.append((collision, "collision"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (candidate, message) in enumerate(cases):
                path = root / f"schedule-{index}.json"
                write_tdm_schedule_storage(path, candidate)
                with self.assertRaisesRegex(ValidationError, message):
                    validate_tdm_schedule(
                        routes,
                        fixture.platform,
                        read_tdm_schedule(path),
                    )

    def test_validate_phase5_keeps_indexed_schedule_lazy(self):
        fixture = (
            combinational_fixture.StaticExactCombinationalCutPartitionTest()
        )
        fixture.setUp()
        _, assignment = fixture._exact_artifacts(dependent_return=True)
        routes = fixture._exact_routes(assignment)
        routes["semantic_contract"] = {
            "schema": assignment["semantic_contract"]["schema"],
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        schedule = build_tdm_schedule(routes, fixture.platform)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes_path = root / "routes.json"
            platform_path = root / "platform.json"
            schedule_path = root / "schedule.json"
            routes_path.write_text("{}", encoding="utf-8")
            platform_path.write_text(
                json.dumps(fixture.platform.to_dict()), encoding="utf-8"
            )
            write_tdm_schedule_storage(schedule_path, schedule)
            with (
                mock.patch(
                    "emuflow.phase5._load_phase3_semantics",
                    return_value=(routes, routes["semantic_contract"]),
                ),
                mock.patch.object(
                    ScheduleRecordSequence,
                    "__iter__",
                    side_effect=AssertionError("schedule was materialized"),
                ),
            ):
                result = validate_phase5(
                    routes_path,
                    platform_path,
                    schedule_path,
                    allow_legacy_generation=True,
                )
            self.assertEqual(result["status"], "pass")

    def test_indexed_schedule_validation_preserves_ratio_and_bank_checks(self):
        fixture = (
            combinational_fixture.StaticExactCombinationalCutPartitionTest()
        )
        fixture.setUp()
        _, assignment = fixture._exact_artifacts(dependent_return=True)
        platform_value = fixture.platform.to_dict()
        platform_value["links"][0].update(
            {
                "capacity_sharing": "shared_bidirectional",
                "bank_count": 4,
                "wires_per_bank": 8,
                "tdm_delay_model": {
                    "model": "linear-ratio-v1",
                    "intercept_ns": 8.0,
                    "ratio_coefficient_ns": 0.5,
                    "min_ratio": 1,
                    "max_ratio": 16,
                    "ratio_quantum": 1,
                },
            }
        )
        platform = Platform.from_dict(platform_value)
        routes = fixture._exact_routes(assignment)
        routes["constraints"] = normalize_route_constraints(
            {
                "schema": "emuflow.system-route-constraints/v1",
                "frame_slots": routes["constraints"]["frame_slots"],
            },
            platform,
        )
        routes["semantic_contract"] = {
            "schema": assignment["semantic_contract"]["schema"],
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality",
            "indexed": True,
        }
        timing_route = routes["routes"][0]
        timing_edge = timing_route["tree_edges"][0]
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 100.0,
                "negative_slack_scale_ns": 100.0,
                "max_clock_period_ns": 100.0,
            },
            "paths": [
                {
                    "path": "ratio-path",
                    "clock_domain": "clk",
                    "clock_period_ns": 100.0,
                    "fixed_delay_ns": 1.0,
                    "cut_nets": [timing_route["net"]],
                    "cut_transitions": [
                        {
                            "net": timing_route["net"],
                            "from": timing_edge["from"],
                            "to": timing_edge["to"],
                        }
                    ],
                }
            ],
        }
        schedule = build_tdm_schedule(routes, platform)
        legacy_timing = reconstruct_tdm_schedule_timing(
            routes, platform, schedule
        )
        candidates = [(schedule, None)]
        bad_ratio = copy.deepcopy(schedule)
        bad_ratio["entries"][0]["tdm_ratio"] = 0
        candidates.append((bad_ratio, "tdm_ratio"))
        bad_bank = copy.deepcopy(schedule)
        entry = bad_bank["entries"][0]
        allocation = next(
            item
            for item in bad_bank["bank_allocations"]
            if (
                item["link"], item["from"], item["to"]
            ) == (entry["link"], entry["from"], entry["to"])
        )
        outside = allocation["lane_offset"] + allocation["lanes"]
        entry["lane"] = outside if outside < 32 else 0
        candidates.append((bad_bank, "allocated direction banks"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (candidate, error) in enumerate(candidates):
                path = root / f"schedule-{index}.json"
                write_tdm_schedule_storage(path, candidate)
                if error is None:
                    indexed = read_tdm_schedule(path)
                    self.assertEqual(
                        validate_tdm_schedule(
                            routes, platform, indexed
                        )["status"],
                        "pass",
                    )
                    with mock.patch.object(
                        ScheduleRecordSequence,
                        "__iter__",
                        side_effect=AssertionError(
                            "timing reconstruction decoded all hops"
                        ),
                    ):
                        self.assertEqual(
                            reconstruct_tdm_schedule_timing(
                                routes, platform, indexed
                            ),
                            legacy_timing,
                        )
                else:
                    with self.assertRaisesRegex(ValidationError, error):
                        validate_tdm_schedule(
                            routes, platform, read_tdm_schedule(path)
                        )

    def _system_inputs(self):
        routes = copy.deepcopy(self.routes)
        routes["timing"] = {
            "schema": "emuflow.sta-paths/v1",
            "normalization": {
                "positive_slack_scale_ns": 20.0,
                "negative_slack_scale_ns": 20.0,
                "max_clock_period_ns": 20.0,
            },
            "paths": [
                {
                    "path": "path0",
                    "clock_domain": "clk",
                    "clock_period_ns": 20.0,
                    "required_time_ns": 18.0,
                    "fixed_delay_ns": 2.0,
                    "cut_nets": ["n0", "d"],
                }
            ],
        }
        timing = reconstruct_tdm_schedule_timing(
            {
                **routes,
                "semantic_contract": self.assignment["semantic_contract"],
            },
            self.platform,
            self.schedule,
        )
        return (
            build_virtual_runtime(self.schedule, self.platform),
            routes,
            {"timing_validation": timing},
        )

    def test_routed_segment_deadlines_pass_with_exact_coverage(self):
        result = build_static_exact_segment_deadlines(
            self.schedule,
            self.assignment["semantic_contract"],
            self.physical,
            self.platform,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["coverage"]["contract_segments"], 3)
        self.assertEqual(result["coverage"]["endpoint_exact_segments"], 3)
        self.assertAlmostEqual(result["worst_source_ready_slack_ns"], 1.0)
        self.assertGreater(result["worst_final_capture_slack_ns"], 0.0)
        validation = validate_static_exact_segment_deadlines(
            result,
            self.schedule,
            self.assignment["semantic_contract"],
            self.physical,
            self.platform,
        )
        self.assertEqual(validation["status"], "pass")

    def test_schedule_references_semantics_without_duplicating_contract(self):
        self.assertNotIn("semantic_contract", self.schedule)
        self.assertEqual(
            self.schedule["semantic_contract_schema"],
            self.assignment["semantic_contract"]["schema"],
        )
        self.assertEqual(
            self.schedule["semantic_contract_sha256"],
            semantic_contract_sha256(self.assignment["semantic_contract"]),
        )
        contract = build_cross_layer_timing_contract(
            self.routes,
            self.schedule,
            self.assignment["semantic_contract"],
        )
        self.assertEqual(
            contract["metrics"]["logic_segments"],
            len(self.assignment["semantic_contract"]["logic_segments"]),
        )

    def test_physical_binding_aggregates_original_members_by_semantic_segment(self):
        physical = self._system_physical()
        launch = copy.deepcopy(
            physical["logic_segment_timing"]["fpga0"]["segments"][0]
        )
        launch.update(
            {
                "id": "physical-launch-second-member",
                "member_path": "path1",
                "delay_ns": 3.5,
            }
        )
        physical["logic_segment_timing"]["fpga0"]["segments"].append(launch)
        contract = build_cross_layer_timing_contract(
            self.routes,
            self.schedule,
            self.assignment["semantic_contract"],
        )
        binding = build_cross_layer_physical_binding(
            contract, self.schedule, physical, self.platform
        )
        bound = next(
            item
            for item in binding["logic_segment_bindings"]
            if item["segment"] == "segment000000"
        )
        self.assertEqual(bound["measured_min_delay_ns"], 3.0)
        self.assertEqual(bound["measured_max_delay_ns"], 3.5)
        self.assertEqual(bound["measurement"], "routed-endpoint-exact")

    def test_deadline_schema_tracks_the_complete_producer_contract(self):
        result = build_static_exact_segment_deadlines(
            self.schedule,
            self.assignment["semantic_contract"],
            self.physical,
            self.platform,
        )
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas/static-exact-segment-deadlines-v2.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema"]["const"], result["schema"])
        self.assertEqual(set(schema["required"]), set(result))
        self.assertEqual(set(schema["properties"]), set(result))
        segment_schema = schema["properties"]["segments"]["items"]
        self.assertTrue(set(segment_schema["required"]).issubset(result["segments"][0]))
        self.assertFalse(set(result["segments"][0]) - set(segment_schema["properties"]))

    def test_source_ready_fails_even_when_aggregate_period_is_large(self):
        physical = copy.deepcopy(self.physical)
        physical["logic_segment_timing"]["fpga0"]["segments"][0][
            "delay_ns"
        ] = 4.5
        result = build_static_exact_segment_deadlines(
            self.schedule,
            self.assignment["semantic_contract"],
            physical,
            self.platform,
        )
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["failed_segments"], ["segment000000"])
        self.assertLess(result["worst_source_ready_slack_ns"], 0.0)

    def test_missing_segment_evidence_is_incomplete_not_pass(self):
        physical = copy.deepcopy(self.physical)
        physical["logic_segment_timing"]["fpga1"]["segments"] = []
        physical["logic_segment_timing"]["fpga1"]["coverage"].update(
            {"segments": 0, "system_paths": 0, "member_paths": 0}
        )
        result = build_static_exact_segment_deadlines(
            self.schedule,
            self.assignment["semantic_contract"],
            physical,
            self.platform,
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["missing_segments"], ["segment000001"])

    def test_tampered_deadline_report_and_contract_binding_are_rejected(self):
        result = build_static_exact_segment_deadlines(
            self.schedule,
            self.assignment["semantic_contract"],
            self.physical,
            self.platform,
        )
        tampered = copy.deepcopy(result)
        tampered["segments"][0]["deadline_slot"] += 1
        with self.assertRaisesRegex(ValidationError, "disagrees"):
            validate_static_exact_segment_deadlines(
                tampered,
                self.schedule,
                self.assignment["semantic_contract"],
                self.physical,
                self.platform,
            )
        physical = copy.deepcopy(self.physical)
        physical["logic_segment_timing"]["fpga0"][
            "semantic_contract_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "another contract"):
            build_static_exact_segment_deadlines(
                self.schedule,
                self.assignment["semantic_contract"],
                physical,
                self.platform,
            )

    def test_system_timing_cannot_hide_missing_or_late_exact_segment(self):
        runtime, routes, phase5 = self._system_inputs()
        physical = self._system_physical()
        passing = build_system_timing(
            runtime,
            routes,
            self.schedule,
            phase5,
            physical,
            self.platform,
            semantic_contract=self.assignment["semantic_contract"],
        )
        self.assertEqual(passing["status"], "pass")
        self.assertEqual(
            passing["static_exact_segment_deadlines"]["status"], "pass"
        )
        path = passing["paths"][0]
        self.assertEqual(
            path["scheduled_link_tdm_model"],
            "sampled-virtual-wire-event-propagation",
        )
        # launch(3 ns) is ready for slot 1 at 4 ns; the two physical links
        # arrive at 12.5 ns and 24.5 ns, then the 7 ns capture segment ends at
        # 31.5 ns. The old separable sum produced 29.5 ns and was incorrect.
        self.assertAlmostEqual(path["system_delay_bound_ns"], 31.5)
        self.assertAlmostEqual(path["target_required_time_ns"], 18.0)
        self.assertAlmostEqual(path["source_required_time_adjustment_ns"], 2.0)
        self.assertAlmostEqual(path["target_clock_slack_bound_ns"], -13.5)
        self.assertAlmostEqual(path["minimum_tx_readiness_slack_ns"], 1.0)
        self.assertEqual(path["sampled_event_timing_status"], "pass")

        missing = copy.deepcopy(physical)
        missing["logic_segment_timing"]["fpga1"]["segments"] = []
        missing["logic_segment_timing"]["fpga1"]["coverage"].update(
            {"segments": 0, "system_paths": 0, "member_paths": 0}
        )
        incomplete = build_system_timing(
            runtime,
            routes,
            self.schedule,
            phase5,
            missing,
            self.platform,
            semantic_contract=self.assignment["semantic_contract"],
        )
        self.assertEqual(incomplete["status"], "incomplete")

        late = copy.deepcopy(physical)
        late["logic_segment_timing"]["fpga0"]["segments"][0][
            "delay_ns"
        ] = 4.5
        failed = build_system_timing(
            runtime,
            routes,
            self.schedule,
            phase5,
            late,
            self.platform,
            semantic_contract=self.assignment["semantic_contract"],
        )
        self.assertEqual(failed["status"], "fail")
        self.assertGreater(
            failed["runtime_clock"]["worst_slack_bound_ns"], 0.0
        )

    def test_physical_link_delay_can_invalidate_a_logically_legal_schedule(self):
        runtime, routes, phase5 = self._system_inputs()
        physical = self._system_physical()
        physical["board_link_timing"] = build_board_link_timing_model(
            self.platform
        )
        for link in physical["board_link_timing"]["links"]:
            if link["from"] == "fpga0" and link["to"] == "fpga1":
                link["delay_bound_ns"] = 10.0
                link["qualification"] = "characterized-upper-bound"
                link["source"] = {
                    "kind": "vendor-characterization",
                    "reference": "fixture",
                }

        result = build_system_timing(
            runtime,
            routes,
            self.schedule,
            phase5,
            physical,
            self.platform,
            semantic_contract=self.assignment["semantic_contract"],
        )
        self.assertEqual(
            result["static_exact_segment_deadlines"]["status"], "pass"
        )
        self.assertEqual(result["status"], "fail")
        path = result["paths"][0]
        self.assertEqual(path["sampled_event_timing_status"], "fail")
        self.assertAlmostEqual(path["minimum_tx_readiness_slack_ns"], -1.0)
        self.assertEqual(result["path_exactness"]["sampled_event_failures"], 1)

    def test_depth_three_event_chain_uses_each_fixed_tx_slot(self):
        entries = {
            f"e{index}": {"id": f"e{index}", "slot": slot}
            for index, slot in enumerate((1, 4, 7))
        }
        hops = [
            {
                "schedule_entry": f"e{index}",
                "demand": f"d{index}",
                "link": "link_0_1",
                "from": "fpga0" if index % 2 == 0 else "fpga1",
                "to": "fpga1" if index % 2 == 0 else "fpga0",
                "tx_endpoint": f"tx{index}",
                "rx_endpoint": f"rx{index}",
                "base_link_delay_ns": 8.0,
                "tdm_wait_slots": 0,
                "tdm_slot_ns": 4.0,
                "link_tdm_delay_ns": 8.0,
            }
            for index in range(3)
        ]
        record = {
            "path": "depth3",
            "cut_nets": ["n0", "n1", "n2"],
            "scheduled_hops": hops,
        }
        segments = [
            {
                "kind": kind,
                "cut_index": index,
                "delay_ns": delay,
                "replace_tx_endpoint": (
                    f"tx{index}" if index < 3 else None
                ),
            }
            for index, (kind, delay) in enumerate(
                (("launch", 3.0), ("transition", 2.0),
                 ("transition", 2.0), ("capture", 2.0))
            )
        ]
        routes = {
            f"n{index}": {"id": f"d{index}"} for index in range(3)
        }
        endpoint_delays = {
            endpoint: 0.5
            for index in range(3)
            for endpoint in (f"tx{index}", f"rx{index}")
        }
        result = _fixed_slot_transport_path_delay(
            record,
            "member0",
            segments,
            endpoint_delays,
            None,
            entries,
            routes,
            commit_slot=10,
            uncertainty_ns=0.5,
        )
        self.assertEqual(result["fixed_slot_event_timing_status"], "pass")
        self.assertAlmostEqual(result["system_delay_bound_ns"], 39.0)
        self.assertAlmostEqual(result["minimum_tx_readiness_slack_ns"], 0.5)
        self.assertAlmostEqual(result["capture_commit_slack_ns"], 1.0)

        late = copy.deepcopy(segments)
        late[1]["delay_ns"] = 4.0
        failed = _fixed_slot_transport_path_delay(
            record,
            "member0",
            late,
            endpoint_delays,
            None,
            entries,
            routes,
            commit_slot=10,
            uncertainty_ns=0.5,
        )
        self.assertEqual(failed["fixed_slot_event_timing_status"], "fail")
        self.assertAlmostEqual(failed["minimum_tx_readiness_slack_ns"], -1.0)

    def test_registered_slot_zero_source_is_stable_before_frame(self):
        entries = {"e0": {"id": "e0", "slot": 0}}
        record = {
            "path": "registered-slot-zero",
            "cut_nets": ["n0"],
            "scheduled_hops": [
                {
                    "schedule_entry": "e0",
                    "demand": "d0",
                    "link": "link_0_1",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tx_endpoint": "tx0",
                    "rx_endpoint": "rx0",
                    "base_link_delay_ns": 2.0,
                    "tdm_wait_slots": 0,
                    "tdm_slot_ns": 20.0,
                    "link_tdm_delay_ns": 2.0,
                }
            ],
        }
        segments = [
            {
                "kind": "launch",
                "cut_index": 0,
                "delay_ns": 8.8,
                "replace_tx_endpoint": "tx0",
            },
            {
                "kind": "capture",
                "cut_index": 1,
                "delay_ns": 1.0,
                "replace_tx_endpoint": None,
            },
        ]
        common = (
            record,
            "member0",
            segments,
            {"tx0": 0.4, "rx0": 0.5},
            None,
            entries,
            {"n0": {"id": "d0"}},
        )
        registered = _fixed_slot_transport_path_delay(
            *common,
            commit_slot=10,
            uncertainty_ns=0.1,
            transport_semantics="registered-boundary",
        )
        self.assertEqual(
            registered["fixed_slot_event_timing_status"], "pass"
        )
        self.assertAlmostEqual(
            registered["minimum_tx_readiness_slack_ns"], 0.0
        )

        sampled = _fixed_slot_transport_path_delay(
            *common,
            commit_slot=10,
            uncertainty_ns=0.1,
            transport_semantics="sampled-virtual-wire",
        )
        self.assertEqual(sampled["fixed_slot_event_timing_status"], "fail")
        self.assertAlmostEqual(
            sampled["minimum_tx_readiness_slack_ns"], -8.9
        )

    def test_clockless_partition_requires_complete_routed_logic_segments(self):
        runtime, routes, phase5 = self._system_inputs()
        physical = self._system_physical()
        for item in physical["fpgas"]:
            item["clock_domain_presence"] = {
                "fabric": True,
                "dut": item["fpga"] != "fpga1",
                "cross": item["fpga"] != "fpga1",
            }
            if item["fpga"] == "fpga1":
                item["clock_domain_delays_ns"].update(
                    {"dut": 0.0, "cross": 0.0}
                )

        passing = build_system_timing(
            runtime,
            routes,
            self.schedule,
            phase5,
            physical,
            self.platform,
            semantic_contract=self.assignment["semantic_contract"],
        )
        self.assertEqual(passing["status"], "pass")

        del physical["logic_segment_timing"]["fpga1"]
        with self.assertRaisesRegex(
            ValidationError, "DUT-clockless partitions without complete"
        ):
            build_system_timing(
                runtime,
                routes,
                self.schedule,
                phase5,
                physical,
                self.platform,
                semantic_contract=self.assignment["semantic_contract"],
            )

    def test_configuration_stable_constant_needs_no_physical_launch(self):
        schedule = copy.deepcopy(self.schedule)
        semantic_contract = copy.deepcopy(
            self.assignment["semantic_contract"]
        )
        launch = next(
            item
            for item in semantic_contract["logic_segments"]
            if item["id"] == "segment000000"
        )
        launch.update(
            {
                "budget_slots": 0,
                "evidence": (
                    "structurally-proven-configuration-stable-constant"
                ),
                "source_semantics": "configuration-stable-constant",
            }
        )
        digest = semantic_contract_sha256(semantic_contract)
        schedule["semantic_contract_schema"] = semantic_contract["schema"]
        schedule["semantic_contract_sha256"] = digest
        physical = copy.deepcopy(self.physical)
        for database in physical["logic_segment_timing"].values():
            database["semantic_contract_sha256"] = digest
        fpga0 = physical["logic_segment_timing"]["fpga0"]
        fpga0["segments"] = [
            item
            for item in fpga0["segments"]
            if item["static_exact_segment_id"] != "segment000000"
        ]
        fpga0["coverage"].update(
            {"segments": 1, "system_paths": 1, "member_paths": 1}
        )

        deadlines = build_static_exact_segment_deadlines(
            schedule, semantic_contract, physical, self.platform
        )
        self.assertEqual(deadlines["status"], "pass")
        self.assertEqual(deadlines["coverage"]["missing_segments"], 0)
        self.assertEqual(
            deadlines["coverage"]["structural_constant_segments"], 1
        )
        constant = next(
            item
            for item in deadlines["segments"]
            if item["id"] == "segment000000"
        )
        self.assertEqual(
            constant["evidence"],
            "structural-configuration-stable-constant",
        )
        self.assertEqual(constant["physical_measurements"], 0)
        self.assertEqual(constant["physical_delay_bound_ns"], 0.0)
        self.assertEqual(
            validate_static_exact_segment_deadlines(
                deadlines,
                schedule,
                semantic_contract,
                physical,
                self.platform,
            )["status"],
            "pass",
        )

        tampered = copy.deepcopy(deadlines)
        constant = next(
            item
            for item in tampered["segments"]
            if item["id"] == "segment000000"
        )
        constant["physical_delay_bound_ns"] = 0.1
        with self.assertRaisesRegex(
            ValidationError, "constant deadline"
        ):
            validate_static_exact_segment_deadlines(
                tampered,
                schedule,
                semantic_contract,
                physical,
                self.platform,
            )


if __name__ == "__main__":
    unittest.main()
