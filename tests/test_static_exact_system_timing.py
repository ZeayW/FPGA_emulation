import copy
import json
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.combinational_cut import semantic_contract_sha256
from emuflow.static_exact_timing import (
    build_static_exact_segment_deadlines,
    validate_static_exact_segment_deadlines,
)
from emuflow.runtime import build_virtual_runtime
from emuflow.system_timing import build_system_timing
from emuflow.tdm import (
    build_tdm_schedule,
    reconstruct_tdm_schedule_timing,
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
        self.routes = fixture._exact_routes(self.assignment)
        self.schedule = build_tdm_schedule(
            self.routes, self.platform
        )
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
                    "fixed_delay_ns": 2.0,
                    "cut_nets": ["n0", "d"],
                }
            ],
        }
        timing = reconstruct_tdm_schedule_timing(
            routes, self.platform, self.schedule
        )
        return (
            build_virtual_runtime(self.schedule, self.platform),
            routes,
            {"timing_validation": timing},
        )

    def test_routed_segment_deadlines_pass_with_exact_coverage(self):
        result = build_static_exact_segment_deadlines(
            self.schedule, self.physical, self.platform
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["coverage"]["contract_segments"], 3)
        self.assertEqual(result["coverage"]["endpoint_exact_segments"], 3)
        self.assertAlmostEqual(result["worst_source_ready_slack_ns"], 1.0)
        self.assertGreater(result["worst_final_capture_slack_ns"], 0.0)
        validation = validate_static_exact_segment_deadlines(
            result, self.schedule, self.physical, self.platform
        )
        self.assertEqual(validation["status"], "pass")

    def test_deadline_schema_tracks_the_complete_producer_contract(self):
        result = build_static_exact_segment_deadlines(
            self.schedule, self.physical, self.platform
        )
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas/static-exact-segment-deadlines-v1.schema.json"
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
            self.schedule, physical, self.platform
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
            self.schedule, physical, self.platform
        )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["missing_segments"], ["segment000001"])

    def test_tampered_deadline_report_and_contract_binding_are_rejected(self):
        result = build_static_exact_segment_deadlines(
            self.schedule, self.physical, self.platform
        )
        tampered = copy.deepcopy(result)
        tampered["segments"][0]["deadline_slot"] += 1
        with self.assertRaisesRegex(ValidationError, "disagrees"):
            validate_static_exact_segment_deadlines(
                tampered, self.schedule, self.physical, self.platform
            )
        physical = copy.deepcopy(self.physical)
        physical["logic_segment_timing"]["fpga0"][
            "semantic_contract_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "another contract"):
            build_static_exact_segment_deadlines(
                self.schedule, physical, self.platform
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
        )
        self.assertEqual(passing["status"], "pass")
        self.assertEqual(
            passing["static_exact_segment_deadlines"]["status"], "pass"
        )

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
        )
        self.assertEqual(failed["status"], "fail")
        self.assertGreater(
            failed["runtime_clock"]["worst_slack_bound_ns"], 0.0
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
            )

    def test_configuration_stable_constant_needs_no_physical_launch(self):
        schedule = copy.deepcopy(self.schedule)
        launch = next(
            item
            for item in schedule["semantic_contract"]["logic_segments"]
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
        digest = semantic_contract_sha256(schedule["semantic_contract"])
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
            schedule, physical, self.platform
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
                deadlines, schedule, physical, self.platform
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
                tampered, schedule, physical, self.platform
            )


if __name__ == "__main__":
    unittest.main()
