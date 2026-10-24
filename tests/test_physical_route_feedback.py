import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.physical_route_feedback import (
    build_physical_route_feedback,
    combine_tdm_and_physical_feedback,
    validate_physical_route_feedback,
)
from emuflow.runtime import build_virtual_runtime
from emuflow.phase4 import run_phase4
from emuflow.io import write_json
from tests.schedule_reference import build_tdm_schedule
from emuflow.tdm_feedback import build_tdm_feedback
from emuflow.timing_routing import compress_sta_paths, normalize_sta_paths
from emuflow.routing import demands_from_assignment
from tests.test_phase7c import Phase7CTest
from tests.native_build import tlr_router


class PhysicalRouteFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture = Phase7CTest()
        fixture.setUp()
        self.platform = fixture.platform
        self.routes = fixture.routes
        self.schedule = build_tdm_schedule(self.routes, self.platform)
        self.runtime = build_virtual_runtime(self.schedule, self.platform)
        self.physical = fixture._physical_summary()
        identities = {}
        timings = {}
        for fpga, kind, endpoint_id, delay in (
            ("fpga0", "tx", "__emuflow_tx_s000000", 2.0),
            ("fpga1", "rx", "__emuflow_rx_s000000", 3.0),
        ):
            identities[fpga] = {
                "schema": "emuflow.boundary-identity/v1",
                "status": "pass",
                "design": "dut",
                "platform": self.platform.name,
                "fpga": fpga,
                "provider": "test",
                "coverage": {
                    "endpoints": 1,
                    "tx": int(kind == "tx"),
                    "rx": int(kind == "rx"),
                    "external_port_nets": 1,
                },
                "endpoints": [
                    {
                        "id": endpoint_id,
                        "kind": kind,
                        "schedule_entry": "s000000",
                    }
                ],
            }
            timings[fpga] = {
                "schema": "emuflow.boundary-timing/v1",
                "status": "pass",
                "design": "dut",
                "platform": self.platform.name,
                "fpga": fpga,
                "provider": "test",
                "qualification": "endpoint-exact",
                "coverage": {
                    "endpoints": 1,
                    "tx": int(kind == "tx"),
                    "rx": int(kind == "rx"),
                },
                "endpoints": [
                    {
                        "id": endpoint_id,
                        "kind": kind,
                        "schedule_entry": "s000000",
                        "delay_ns": delay,
                        "start_object": "start",
                        "end_object": "end",
                        "measurement": (
                            "logical-source-to-tx-port"
                            if kind == "tx"
                            else "rx-port-to-shadow-capture"
                        ),
                    }
                ],
            }
        self.physical["boundary_identities"] = identities
        self.physical["boundary_timing"] = timings

    def test_feedback_reconstructs_boundary_prices_and_combines(self) -> None:
        feedback = build_physical_route_feedback(
            self.runtime,
            self.routes,
            self.platform,
            self.schedule,
            self.physical,
        )
        checked = validate_physical_route_feedback(
            self.runtime,
            self.routes,
            self.platform,
            self.schedule,
            self.physical,
            feedback,
        )
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(checked["maximum_path_boundary_delay_ns"], 5.0)
        tdm = build_tdm_feedback(
            self.routes, self.platform, self.schedule
        )
        combined = combine_tdm_and_physical_feedback(
            tdm, feedback, physical_weight=2.0
        )
        active = next(
            domain
            for domain in combined["domains"]
            if domain["scheduled_bit_hops"]
        )
        self.assertGreater(
            active["routing_price"], active["schedule_routing_price"]
        )
        self.assertEqual(combined["provider"], tdm["provider"])

        tampered = copy.deepcopy(feedback)
        tampered["domains"][0]["physical_routing_price"] += 1.0
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_physical_route_feedback(
                self.runtime,
                self.routes,
                self.platform,
                self.schedule,
                self.physical,
                tampered,
            )

    def test_phase4_revalidates_physical_sources_before_routing(self) -> None:
        feedback = build_physical_route_feedback(
            self.runtime,
            self.routes,
            self.platform,
            self.schedule,
            self.physical,
        )
        tdm = build_tdm_feedback(
            self.routes, self.platform, self.schedule
        )
        assignment = {
            "schema": "emuflow.partition-assignment/v1",
            "design": "dut",
            "platform": self.platform.name,
            "cut_nets": [
                {
                    "net": "n0",
                    "cut_class": "register_output",
                    "source_fpgas": ["fpga0"],
                    "sink_fpgas": ["fpga1"],
                    "sink_endpoints": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = {
                "assignment.json": assignment,
                "platform.json": self.platform.to_dict(),
                "timing.json": compress_sta_paths(
                    normalize_sta_paths(
                        {
                            "schema": "emuflow.sta-paths/v1",
                            "design": "dut",
                            "paths": [
                                {
                                    "id": "system-critical",
                                    "clock_domain": "clk",
                                    "clock_period_ns": 20.0,
                                    "slack_ns": 10.0,
                                    "fixed_delay_ns": 2.0,
                                    "cut_nets": ["n0"],
                                }
                            ],
                        },
                        demands_from_assignment(assignment, self.platform),
                    )
                ),
                "routes.json": self.routes,
                "schedule.json": self.schedule,
                "tdm.json": tdm,
                "runtime.json": self.runtime,
                "physical.json": self.physical,
                "feedback.json": feedback,
            }
            for name, value in values.items():
                write_json(root / name, value)
            report = run_phase4(
                assignment_path=root / "assignment.json",
                platform_path=root / "platform.json",
                output_dir=root / "phase4",
                provider="timing-aware-global-candidate-v1",
                timing_paths_path=root / "timing.json",
                router=str(tlr_router()),
                tdm_feedback_path=root / "tdm.json",
                tdm_feedback_routes_path=root / "routes.json",
                tdm_feedback_schedule_path=root / "schedule.json",
                physical_feedback_path=root / "feedback.json",
                physical_feedback_runtime_path=root / "runtime.json",
                physical_feedback_summary_path=root / "physical.json",
            )
            self.assertEqual(report["status"], "pass")
            self.assertIn("physical_validation", report["tdm_feedback"])
            broken = copy.deepcopy(self.physical)
            broken["boundary_timing"]["fpga0"]["endpoints"][0][
                "delay_ns"
            ] += 1.0
            write_json(root / "physical.json", broken)
            with self.assertRaisesRegex(ValidationError, "does not match"):
                run_phase4(
                    assignment_path=root / "assignment.json",
                    platform_path=root / "platform.json",
                    output_dir=root / "broken",
                    provider="timing-aware-global-candidate-v1",
                    timing_paths_path=root / "timing.json",
                    router=str(tlr_router()),
                    tdm_feedback_path=root / "tdm.json",
                    tdm_feedback_routes_path=root / "routes.json",
                    tdm_feedback_schedule_path=root / "schedule.json",
                    physical_feedback_path=root / "feedback.json",
                    physical_feedback_runtime_path=root / "runtime.json",
                    physical_feedback_summary_path=root / "physical.json",
                )


if __name__ == "__main__":
    unittest.main()
