import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import _build_parser
from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.local_path_timing import path_id_set_sha256
from emuflow.routing_tdm_comparison import (
    build_system_route_tdm_ab_comparison,
    build_system_route_tdm_scale_comparison,
    validate_system_route_tdm_ab_comparison,
    validate_system_route_tdm_scale_comparison,
)
from emuflow.tdm import TDM_ACADEMIC_SCHEDULE_PROVIDER, TDM_BASELINE_PROVIDER
from emuflow.timing_routing import GLOBAL_CANDIDATE_PROVIDER, ROUTE_TDM_PROVIDER


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _physical(value: float, tool: Path):
    architecture_sha = "a" * 64
    return {
        "backend": {
            "schema": "emuflow.physical-backend/v1",
            "id": "open",
            "implementation_engine": "vpr-openparf-vpr",
            "timing_engine": "vpr",
            "architecture_class": "public-academic",
            "source_model": "source-complete",
            "qualification": "academic-architecture-not-vendor-signoff",
            "capabilities": {
                "packing": True,
                "placement": True,
                "routing": True,
                "timing": True,
                "bitstream": False,
            },
        },
        "architecture": {
            "status": "pass",
            "mode": "provided",
            "path": "/flow-local/architecture.xml",
            "sha256": architecture_sha,
        },
        "execution": {
            "requested_workers": 1,
            "effective_workers": 1,
            "ordering": "boarddb-fpga-order",
            "pack_place_resume": False,
        },
        "expected_fpgas": ["fpga0"],
        "fpgas": [
            {
                "fpga": "fpga0",
                "critical_path_ns": 10.0 - value,
                "stages": {
                    "vpr_pack_place": {
                        "architecture": {"sha256": architecture_sha},
                        "configuration": {"seed": 1},
                        "command": [str(tool), "--pack", "--place"],
                    },
                    "vpr_route": {
                        "architecture": {"sha256": architecture_sha},
                        "configuration": {
                            "route_channel_width": 300,
                            "retain_rr_graph": False,
                        },
                        "command": [str(tool), "--route"],
                        "metrics": {"wirelength": 100 - int(value)},
                    },
                },
                "physical_result": {
                    "timing": {
                        "wns_ns": value,
                        "tns_ns": min(0.0, value * 2.0),
                        "failing_endpoints": 1 if value < 0 else 0,
                        "failing_endpoint_constraints": 1 if value < 0 else 0,
                    },
                    "closure": {"unrouted_nets": 0, "drc_violations": 0},
                },
            }
        ]
    }


class RoutingTdmComparisonTest(unittest.TestCase):
    def test_scale_cli_requires_and_parses_v2_sources_and_runtimes(self):
        arguments = _build_parser().parse_args(
            [
                "multi-fpga", "compare-routing-tdm-scale",
                "--assignment", "assignment.json",
                "--platform", "platform.json",
                "--route-constraints", "constraints.json",
                "--timing-paths", "timing.json",
                "--baseline-route", "baseline-route",
                "--baseline-tdm", "baseline-tdm",
                "--upgrade-route", "upgrade-route",
                "--upgrade-tdm", "upgrade-tdm",
                "--baseline-runtime-seconds", "12.5",
                "--upgrade-runtime-seconds", "9.25",
                "--output", "scale.json",
            ]
        )
        self.assertEqual(arguments.route_constraints, Path("constraints.json"))
        self.assertEqual(arguments.baseline_runtime_seconds, 12.5)
        self.assertEqual(arguments.upgrade_runtime_seconds, 9.25)

    def test_builds_independently_replayed_scale_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = {}
            for name in ("assignment", "platform", "constraints", "timing"):
                path = root / f"{name}.json"
                write_json(path, {"name": name})
                sources[name] = path
            arms = {}
            for label, route_provider, tdm_provider in (
                ("baseline", ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER),
                ("upgrade", GLOBAL_CANDIDATE_PROVIDER, TDM_ACADEMIC_SCHEDULE_PROVIDER),
            ):
                route_root = root / f"{label}-route"
                tdm_root = root / f"{label}-tdm"
                route_root.mkdir()
                tdm_root.mkdir()
                write_json(route_root / "routes.json", {"provider": route_provider})
                write_json(
                    route_root / "route_constraints.normalized.json",
                    {"name": "normalized-constraints"},
                )
                write_json(tdm_root / "schedule.json", {"provider": tdm_provider})
                if label == "upgrade":
                    write_json(tdm_root / "ratio_plan.json", {"provider": "ratio"})
                arms[label] = (route_root, tdm_root)
            with (
                patch(
                    "emuflow.routing_tdm_comparison.validate_phase4",
                    return_value={
                        "status": "pass", "routed_sinks": 17,
                        "tree_edges": 19, "total_link_bit_hops": 29,
                        "max_link_utilization": 0.5, "overloaded_links": 0,
                        "worst_slack_ns": -3.0,
                        "worst_normalized_slack": -0.3,
                        "estimated_worst_tdm_slack_ns": -5.0,
                    },
                ),
                patch(
                    "emuflow.routing_tdm_comparison.validate_phase5",
                    return_value={
                        "status": "pass", "scheduled_bit_hops": 23,
                        "frame_slots": 32, "completion_slot": 12,
                        "max_domain_utilization": 0.75, "collisions": 0,
                        "timing": {
                            "status": "pass", "worst_slack_ns": -2.0,
                            "worst_normalized_slack": -0.2,
                            "p01_normalized_slack": -0.1,
                            "median_normalized_slack": 0.3,
                            "negative_slack_paths": 2,
                        },
                    },
                ),
                patch(
                    "emuflow.routing_tdm_comparison.Platform.load",
                    return_value=object(),
                ),
                patch(
                    "emuflow.routing_tdm_comparison.load_route_constraints",
                    return_value={"name": "normalized-constraints"},
                ),
            ):
                report = build_system_route_tdm_scale_comparison(
                    sources["assignment"], sources["platform"],
                    sources["constraints"], sources["timing"],
                    *arms["baseline"], *arms["upgrade"], root / "scale.json",
                    baseline_runtime_seconds=10.0,
                    upgrade_runtime_seconds=8.0,
                )
            self.assertEqual(report["validation"]["baseline_routed_sinks"], 17)
            self.assertEqual(report["runtime_delta_seconds"], -2.0)
            self.assertEqual(
                report["delta_upgrade_minus_baseline"]["tdm_worst_slack_ns"],
                0.0,
            )
            broken = copy.deepcopy(report)
            broken["arms"]["upgrade"]["tdm_provider"] = "wrong"
            with self.assertRaises(ValidationError):
                validate_system_route_tdm_scale_comparison(broken)
            broken = copy.deepcopy(report)
            broken["frozen_upstream"]["route_constraints"] = "f" * 64
            # A different valid digest is structurally valid in the detached
            # report, but changing a reconstructed metric must still fail.
            broken["arms"]["upgrade"]["metrics"]["tdm_worst_slack_ns"] = 1.0
            with self.assertRaisesRegex(ValidationError, "not reconstructed"):
                validate_system_route_tdm_scale_comparison(broken)
            broken = copy.deepcopy(report)
            broken["arms"]["upgrade"]["metrics"]["tdm_worst_slack_ns"] = 1.0
            broken["delta_upgrade_minus_baseline"]["tdm_worst_slack_ns"] = 3.0
            with self.assertRaisesRegex(ValidationError, "not reconstructed"):
                validate_system_route_tdm_scale_comparison(broken)

            write_json(
                arms["upgrade"][0] / "route_constraints.normalized.json",
                {"name": "different-constraints"},
            )
            with self.assertRaisesRegex(ValidationError, "constraints differ"):
                with (
                    patch(
                        "emuflow.routing_tdm_comparison.validate_phase4",
                        return_value=report["arms"]["upgrade"]["route_validation"],
                    ),
                    patch(
                        "emuflow.routing_tdm_comparison.validate_phase5",
                        return_value=report["arms"]["upgrade"]["tdm_validation"],
                    ),
                    patch(
                        "emuflow.routing_tdm_comparison.Platform.load",
                        return_value=object(),
                    ),
                    patch(
                        "emuflow.routing_tdm_comparison.load_route_constraints",
                        return_value={"name": "normalized-constraints"},
                    ),
                ):
                    build_system_route_tdm_scale_comparison(
                        sources["assignment"], sources["platform"],
                        sources["constraints"], sources["timing"],
                        *arms["baseline"], *arms["upgrade"],
                        root / "tampered-scale.json",
                        baseline_runtime_seconds=10.0,
                        upgrade_runtime_seconds=8.0,
                    )

    def _report(self, root: Path, route: str, tdm: str, value: float):
        tool = root.parent / "physical-tool"
        tool.write_bytes(b"pinned physical tool\n")
        artifacts = {}
        for key in (
            "platform", "emuir", "partition_constraints",
            "route_constraints", "assignment", "timing_path_database",
            "partition_net_weights", "routes",
        ):
            path = root / f"{key}.json"
            write_json(path, ({"schema": "emuflow.sta-path-database/v1", "paths": [
                {"id": "member-a"}, {"id": "member-b"}]} if key == "timing_path_database" else {"kind": key}))
            artifacts[key] = {"path": path.name, "sha256": _sha256(path)}
        global_slack = -2.0 + value
        return {
            "status": "pass",
            "stages": {
                "frontend": {"design": "d", "platform": "p"},
                "partition": {"provider": "partition", "design": "d", "platform": "p"},
                "system_route": {
                    "provider": route,
                    "design": "d",
                    "platform": "p",
                    "validation": {
                        "demands": 1,
                        "routed_sinks": 1,
                        "tree_edges": 1,
                        "total_link_bit_hops": 1,
                        "max_link_utilization": 0.5,
                        "overloaded_links": 0,
                        "estimated_worst_tdm_slack_ns": value,
                    },
                },
                "tdm": {
                    "provider": tdm,
                    "design": "d",
                    "platform": "p",
                    "validation": {
                        "frame_slots": 8,
                        "completion_slot": 1,
                        "max_domain_utilization": 0.5,
                        "scheduled_bit_hops": 1,
                        "collisions": 0,
                    },
                },
                "split": {
                    "provider": "deterministic-cut-shadow-split-v1",
                    "design": "d",
                    "platform": "p",
                },
            },
            "artifacts": artifacts,
            "physical": _physical(value, tool),
            "runtime": {
                "system_timing": {
                    "schema": "emuflow.system-timing/v2",
                    "status": "pass",
                    "timing_scope": "whole-original-design",
                    "summary": {
                        "original_paths": 2,
                        "original_local_paths": 1,
                        "original_cross_fpga_paths": 1,
                        "compressed_representative_paths": 1,
                        "original_path_coverage": 1.0,
                        "original_path_ids_sha256": path_id_set_sha256(["member-a", "member-b"]),
                    },
                    "source_binding": {
                        "path_database_sha256": artifacts[
                            "timing_path_database"
                        ]["sha256"],
                        "original_ir_sha256": artifacts["emuir"]["sha256"],
                        "assignment_sha256": artifacts["assignment"]["sha256"],
                        "routes_sha256": artifacts["routes"]["sha256"],
                        "original_paths": 2,
                        "original_path_ids_sha256": path_id_set_sha256(["member-a", "member-b"]),
                    },
                    "target_clock": {
                        "worst_slack_bound_ns": global_slack,
                        "tns_bound_ns": 2.0 * min(0.0, global_slack),
                        "negative_slack_paths": 2 if global_slack < 0.0 else 0,
                    },
                    "runtime_clock": {
                        "worst_slack_bound_ns": 10.0 + value,
                        "tns_bound_ns": 0.0,
                        "negative_slack_paths": 0,
                    },
                    "path_exactness": {
                        "physical_logic_segments": True,
                        "physical_logic_segment_bounds": True,
                        "endpoint_exact_logic_paths": 2,
                        "cone_bound_logic_paths": 0,
                        "fallback_logic_paths": 0,
                    },
                    "paths": [
                        {
                            "path": member,
                            "target_clock_slack_bound_ns": global_slack,
                            "runtime_clock_slack_bound_ns": 10.0 + value,
                            "physical_logic_segments_exact": True,
                            "physical_logic_segments_cone_bound": False,
                            "physical_logic_model": model,
                        }
                        for member, model in (
                            (
                                "member-a",
                                "routed-selected-path-chain-exact",
                            ),
                            ("member-b", "routed-staging-chain-exact"),
                        )
                    ],
                }
            },
        }

    def test_builds_source_bound_complete_phase7_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0)
            up_report = self._report(upgrade, GLOBAL_CANDIDATE_PROVIDER, TDM_ACADEMIC_SCHEDULE_PROVIDER, -0.25)
            write_json(baseline / "multi-fpga-flow-report.json", base_report)
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            output = root / "comparison.json"
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(baseline, upgrade, output)
            self.assertEqual(
                result["validation"]["target_global_wns_improvement_ns"],
                0.75,
            )
            self.assertEqual(
                result["validation"]["target_global_tns_improvement_ns"],
                1.5,
            )
            self.assertEqual(
                result["global_timing_improvement"][
                    "target_global_wns_ns"
                ]["negative_slack_deficit_reduction_percent"],
                25.0,
            )
            self.assertEqual(
                result["global_timing_improvement"][
                    "target_global_wns_ns"
                ]["closure_transition"],
                "remained-open",
            )
            self.assertEqual(
                result["arms"]["baseline"]["global_timing"][
                    "physical_logic_exact_paths"
                ],
                2,
            )
            self.assertTrue(output.is_file())
            tampered = copy.deepcopy(result)
            tampered["physical_delta_upgrade_minus_baseline"]["worst_wns_ns"] = 99
            with self.assertRaises(ValidationError):
                validate_system_route_tdm_ab_comparison(tampered)
            tampered = copy.deepcopy(result)
            tampered["arms"]["upgrade"]["global_timing"][
                "physical_logic_exact_paths"
            ] = 1
            with self.assertRaisesRegex(
                ValidationError, "physical logic exactness"
            ):
                validate_system_route_tdm_ab_comparison(tampered)

    def test_rejects_mislabeled_conservative_local_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade,
                GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER,
                -0.25,
            )
            for report in (base_report, up_report):
                local = report["runtime"]["system_timing"]["paths"][0]
                local["physical_logic_model"] = (
                    "routed-endpoint-longest-path-conservative"
                )
                # This was the legacy bug: a conservative endpoint traversal
                # was incorrectly presented as an exact selected path.
                self.assertTrue(local["physical_logic_segments_exact"])
            write_json(baseline / "multi-fpga-flow-report.json", base_report)
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(
                    ValidationError, "physical logic exactness is inconsistent"
                ):
                    build_system_route_tdm_ab_comparison(
                        baseline, upgrade, root / "comparison.json"
                    )

    def test_reports_closure_and_na_percentage_for_closed_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, 2.5
            )
            up_report = self._report(
                upgrade,
                GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER,
                3.0,
            )
            write_json(
                baseline / "multi-fpga-flow-report.json", base_report
            )
            write_json(
                upgrade / "multi-fpga-flow-report.json", up_report
            )
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(
                    baseline, upgrade, root / "comparison.json"
                )
            wns = result["global_timing_improvement"][
                "target_global_wns_ns"
            ]
            self.assertIsNone(
                wns["negative_slack_deficit_reduction_percent"]
            )
            self.assertEqual(wns["closure_transition"], "remained-closed")

            tampered = copy.deepcopy(result)
            tampered["global_timing_improvement"][
                "target_global_wns_ns"
            ]["closure_transition"] = "closed"
            with self.assertRaisesRegex(ValidationError, "improvement disagrees"):
                validate_system_route_tdm_ab_comparison(tampered)

    def test_reports_negative_slack_crossing_as_timing_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            write_json(
                baseline / "multi-fpga-flow-report.json",
                self._report(
                    baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
                ),
            )
            write_json(
                upgrade / "multi-fpga-flow-report.json",
                self._report(
                    upgrade,
                    GLOBAL_CANDIDATE_PROVIDER,
                    TDM_ACADEMIC_SCHEDULE_PROVIDER,
                    2.5,
                ),
            )
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(
                    baseline, upgrade, root / "comparison.json"
                )
            wns = result["global_timing_improvement"][
                "target_global_wns_ns"
            ]
            self.assertEqual(wns["closure_transition"], "closed")
            self.assertEqual(
                wns["negative_slack_deficit_reduction_percent"], 100.0
            )

    def test_rejects_cross_fpga_subset_mislabeled_as_global(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade, GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER, -0.25,
            )
            base_report["runtime"]["system_timing"]["timing_scope"] = (
                "cross-fpga-path-subset"
            )
            write_json(baseline / "multi-fpga-flow-report.json", base_report)
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(
                    ValidationError, "cross-FPGA-only subset"
                ):
                    build_system_route_tdm_ab_comparison(
                        baseline, upgrade, root / "comparison.json"
                    )

    def test_rejects_whole_design_timing_bound_to_another_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade, GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER, -0.25,
            )
            base_report["runtime"]["system_timing"]["source_binding"][
                "routes_sha256"
            ] = "0" * 64
            write_json(baseline / "multi-fpga-flow-report.json", base_report)
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(
                    ValidationError, "routes_sha256.*disagrees"
                ):
                    build_system_route_tdm_ab_comparison(
                        baseline, upgrade, root / "comparison.json"
                    )

    def test_normalizes_only_ephemeral_opensta_staging_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade, GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER, -0.25,
            )
            for flow_root, report, staging in (
                (baseline, base_report, "emuflow-opensta-alpha"),
                (upgrade, up_report, "emuflow-opensta-beta"),
            ):
                path = flow_root / "timing_path_database.json"
                write_json(path, {
                    "schema": "emuflow.sta-path-database/v1",
                    "source": {"input": f"/tmp/{staging}/paths.tsv"},
                    "paths": [{"id": member, "arrival_ns": 1.25}
                              for member in ("member-a", "member-b")],
                })
                report["artifacts"]["timing_path_database"]["sha256"] = _sha256(path)
                report["runtime"]["system_timing"]["source_binding"][
                    "path_database_sha256"
                ] = _sha256(path)
                write_json(flow_root / "multi-fpga-flow-report.json", report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(
                    baseline, upgrade, root / "comparison.json"
                )
            self.assertEqual(result["status"], "pass")

    def test_rejects_mixed_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0)
            up_report = self._report(upgrade, GLOBAL_CANDIDATE_PROVIDER, TDM_ACADEMIC_SCHEDULE_PROVIDER, -0.25)
            (upgrade / "assignment.json").write_text("other\n", encoding="utf-8")
            up_report["artifacts"]["assignment"]["sha256"] = _sha256(upgrade / "assignment.json")
            write_json(baseline / "multi-fpga-flow-report.json", base_report)
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaises(ValidationError):
                    build_system_route_tdm_ab_comparison(baseline, upgrade, root / "comparison.json")

    def test_rejects_mixed_boarddb_and_physical_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade,
                GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER,
                -0.25,
            )
            write_json(upgrade / "platform.json", {"name": "other-board"})
            up_report["artifacts"]["platform"]["sha256"] = _sha256(
                upgrade / "platform.json"
            )
            write_json(baseline / "multi-fpga-flow-report.json", base_report)
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(ValidationError, "platform.*differs"):
                    build_system_route_tdm_ab_comparison(
                        baseline, upgrade, root / "board-mixed.json"
                    )

            write_json(upgrade / "platform.json", {"kind": "platform"})
            up_report["artifacts"]["platform"]["sha256"] = _sha256(
                upgrade / "platform.json"
            )
            up_report["physical"]["fpgas"][0]["stages"][
                "vpr_pack_place"
            ]["configuration"]["seed"] = 9
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(
                    ValidationError, "architecture, tools, seed, workers"
                ):
                    build_system_route_tdm_ab_comparison(
                        baseline, upgrade, root / "seed-mixed.json"
                    )

    def test_reads_pre_v5_canonical_frozen_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade,
                GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER,
                -0.25,
            )
            canonical = {
                "platform": "frontend/phase1/platform.normalized.json",
                "partition_constraints": "partition/constraints.normalized.json",
                "route_constraints": (
                    "system-route/route_constraints.normalized.json"
                ),
            }
            for flow_root, report in (
                (baseline, base_report),
                (upgrade, up_report),
            ):
                for key, relative in canonical.items():
                    source = flow_root / report["artifacts"][key]["path"]
                    destination = flow_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(source.read_bytes())
                    del report["artifacts"][key]
                write_json(flow_root / "multi-fpga-flow-report.json", report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(
                    baseline, upgrade, root / "legacy.json"
                )
            self.assertEqual(result["validation"]["status"], "pass")
            self.assertEqual(
                set(result["frozen_upstream"]),
                {
                    "platform",
                    "emuir",
                    "partition_constraints",
                    "route_constraints",
                    "assignment",
                    "timing_path_database",
                    "partition_net_weights",
                },
            )

    def test_rejects_resealed_reproducibility_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            write_json(
                baseline / "multi-fpga-flow-report.json",
                self._report(
                    baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
                ),
            )
            write_json(
                upgrade / "multi-fpga-flow-report.json",
                self._report(
                    upgrade,
                    GLOBAL_CANDIDATE_PROVIDER,
                    TDM_ACADEMIC_SCHEDULE_PROVIDER,
                    -0.25,
                ),
            )
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(
                    baseline, upgrade, root / "comparison.json"
                )
            tampered = copy.deepcopy(result)
            tampered["arms"]["baseline"]["physical_reproducibility"][
                "vpr_pack_place_seed"
            ]["fpga0"] = 17
            tampered["arms"]["upgrade"]["physical_reproducibility"][
                "vpr_pack_place_seed"
            ]["fpga0"] = 17
            with self.assertRaisesRegex(
                ValidationError, "reproducibility evidence disagrees"
            ):
                validate_system_route_tdm_ab_comparison(tampered)

    def test_normalizes_only_each_flow_root_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            upgrade = root / "upgrade"
            baseline.mkdir()
            upgrade.mkdir()
            base_report = self._report(
                baseline, ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER, -1.0
            )
            up_report = self._report(
                upgrade,
                GLOBAL_CANDIDATE_PROVIDER,
                TDM_ACADEMIC_SCHEDULE_PROVIDER,
                -0.25,
            )
            for flow_root, report in ((baseline, base_report), (upgrade, up_report)):
                emuir = flow_root / "emuir.json"
                write_json(
                    emuir,
                    {"source": str(flow_root / "frontend/phase1/frontend.json")},
                )
                report["artifacts"]["emuir"]["sha256"] = _sha256(emuir)
                report["runtime"]["system_timing"]["source_binding"][
                    "original_ir_sha256"
                ] = _sha256(emuir)
                write_json(flow_root / "multi-fpga-flow-report.json", report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                result = build_system_route_tdm_ab_comparison(
                    baseline, upgrade, root / "comparison.json"
                )
            self.assertEqual(result["validation"]["status"], "pass")
            write_json(upgrade / "emuir.json", {"source": "/unrelated/input.json"})
            up_report["artifacts"]["emuir"]["sha256"] = _sha256(
                upgrade / "emuir.json"
            )
            up_report["runtime"]["system_timing"]["source_binding"][
                "original_ir_sha256"
            ] = _sha256(upgrade / "emuir.json")
            write_json(upgrade / "multi-fpga-flow-report.json", up_report)
            with patch(
                "emuflow.routing_tdm_comparison.validate_multi_fpga_flow_report",
                return_value={"status": "pass"},
            ):
                with self.assertRaisesRegex(ValidationError, "emuir.*differs"):
                    build_system_route_tdm_ab_comparison(
                        baseline, upgrade, root / "second.json"
                    )


if __name__ == "__main__":
    unittest.main()
