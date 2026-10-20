import json
import tempfile
import unittest
from pathlib import Path

from emuflow.contest_eda2025 import (
    evaluate_eda2025_routes,
    import_eda2025_instance,
    materialize_eda2025_rtl_boarddb,
    optimize_eda2025_routing,
    optimize_eda2025_topology,
)
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.phase4 import run_phase4
from emuflow.timing_routing import GLOBAL_CANDIDATE_PROVIDER
from tests.native_build import eda2025_topology_optimizer, tlr_router


INFO = """\
F1 3
F2 3
F3 3
F4 3
"""

NETS = """\
g1 1 g2 g3
g4 1 g7
g5 1 g6
"""

TOPOLOGY = """\
F1: 0,1,0,1
F2: 1,0,1,0
F3: 0,1,0,1
F4: 1,0,1,0
"""

ASSIGNMENT = """\
F1: g2 g4
F2: g7
F3: g1 g6
F4: g3 g5
"""


class Eda2025ContestAdapterTest(unittest.TestCase):
    def _import(self, root: Path):
        source = root / "source"
        source.mkdir()
        files = {
            "info": ("design.info", INFO),
            "net": ("design.net", NETS),
            "topology": ("design.topo", TOPOLOGY),
            "assignment": ("design.fpga.out", ASSIGNMENT),
        }
        paths = {}
        for key, (name, content) in files.items():
            paths[key] = source / name
            paths[key].write_text(content, encoding="utf-8")
        output = root / "normalized"
        report = import_eda2025_instance(
            info_path=paths["info"],
            net_path=paths["net"],
            topology_path=paths["topology"],
            assignment_path=paths["assignment"],
            output_dir=output,
            name="eda2025_sample",
        )
        return report, output

    def test_published_sample_runs_through_native_cpp_router_and_checker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imported, normalized = self._import(root)
            self.assertEqual(imported["fpgas"], 4)
            self.assertEqual(imported["nets"], 3)
            self.assertEqual(imported["cut_nets"], 3)

            routed = root / "routed"
            phase4 = run_phase4(
                assignment_path=normalized / "partition_assignment.json",
                platform_path=normalized / "boarddb.json",
                output_dir=routed,
                constraints_path=normalized / "route_constraints.json",
                timing_paths_path=normalized / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            self.assertEqual(phase4["status"], "pass")
            self.assertEqual(
                phase4["provider"],
                "timing-aware-global-candidate-v1",
            )
            routes_artifact = read_json(routed / "routes.json")
            opposing_pair = next(
                record
                for record in routes_artifact["link_utilization"]
                if record["link"] == "contest_link_002_003"
            )
            self.assertEqual(
                opposing_pair["key"], "contest_link_002_003:shared"
            )
            self.assertEqual(opposing_pair["used_bits"], 2)

            evaluation = evaluate_eda2025_routes(
                normalized / "contest_instance.json",
                routed / "routes.json",
                runtime_seconds=18.0,
                official_output_dir=root / "official",
            )
            self.assertEqual(evaluation["status"], "pass")
            self.assertEqual(evaluation["metrics"]["routed_cut_nets"], 3)
            self.assertEqual(evaluation["metrics"]["max_tdm_ratio"], 8)
            self.assertAlmostEqual(
                evaluation["metrics"]["worst_path_delay_ns"], 71.2
            )
            self.assertAlmostEqual(
                evaluation["metrics"]["contest_score"],
                71.2 * (1.0 + 0.2 * 18.0 / 3600.0),
            )
            with self.assertRaisesRegex(ValidationError, "finite"):
                evaluate_eda2025_routes(
                    normalized / "contest_instance.json",
                    routed / "routes.json",
                    runtime_seconds=float("inf"),
                )
            official_routes = (root / "official" / "design.route.out").read_text()
            self.assertEqual(official_routes.count("[net "), 3)
            self.assertIn("[net 1]", official_routes)
            self.assertEqual(
                (root / "official" / "design.newtopo").read_text(),
                TOPOLOGY,
            )
            topology_out = root / "topology"
            topology_report = optimize_eda2025_topology(
                instance_path=normalized / "contest_instance.json",
                routes_path=routed / "routes.json",
                output_dir=topology_out,
                executable=str(eda2025_topology_optimizer()),
                enable_shortcuts=True,
            )
            self.assertEqual(topology_report["metrics"]["changed_channels"], 1)
            self.assertEqual(
                topology_report["changes"][0]["fpgas"], ["F1", "F3"]
            )
            shortcut_routed = root / "shortcut-routed"
            run_phase4(
                assignment_path=topology_out / "normalized" / "partition_assignment.json",
                platform_path=topology_out / "normalized" / "boarddb.json",
                output_dir=shortcut_routed,
                constraints_path=topology_out / "normalized" / "route_constraints.json",
                timing_paths_path=topology_out / "normalized" / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            shortcut_evaluation = evaluate_eda2025_routes(
                normalized / "contest_instance.json",
                shortcut_routed / "routes.json",
                new_topology_path=topology_out / "design.newtopo",
            )
            self.assertAlmostEqual(
                shortcut_evaluation["metrics"]["worst_path_delay_ns"], 35.6
            )
            portfolio = optimize_eda2025_routing(
                instance_path=normalized / "contest_instance.json",
                routes_path=routed / "routes.json",
                output_dir=root / "portfolio",
                router=str(tlr_router()),
                provider=GLOBAL_CANDIDATE_PROVIDER,
                topology_optimizer=str(eda2025_topology_optimizer()),
            )
            self.assertEqual(
                portfolio["selected"]["name"], "capacity-and-shortcuts"
            )
            self.assertTrue(portfolio["selected"]["improved"])
            self.assertEqual(portfolio["selected"]["rounds_completed"], 1)
            self.assertEqual(
                portfolio["termination"], "topology_change_budget_exhausted"
            )
            self.assertAlmostEqual(
                portfolio["selected"]["worst_path_delay_ns"], 35.6
            )

    def test_contest_topology_materializes_an_rtl_capable_boarddb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            output = root / "rtl-boarddb.json"
            repository = Path(__file__).resolve().parents[1]
            report = materialize_eda2025_rtl_boarddb(
                instance_path=normalized / "contest_instance.json",
                device_template_path=(
                    repository / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                ),
                output_path=output,
                name="eda2025_case01_academic_rtl",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["fpgas"], 4)
            self.assertEqual(report["links"], 4)
            self.assertEqual(report["contest_channels"], 4)
            boarddb = read_json(output)
            self.assertEqual(boarddb["platform"]["kind"], "virtual")
            self.assertEqual(
                boarddb["platform"]["provenance"]["interconnect"]["instance"],
                "eda2025_sample",
            )
            self.assertEqual(
                boarddb["platform"]["provenance"]["device_template"]["platform"],
                "academic_vtr_4fpga_mesh",
            )
            self.assertEqual(
                boarddb["platform"]["provenance"]["device_template"]["fpga"],
                "fpga0",
            )
            self.assertEqual(
                {fpga["id"] for fpga in boarddb["fpgas"]},
                {"F1", "F2", "F3", "F4"},
            )
            self.assertEqual(
                {fpga["capacity"]["lut"] for fpga in boarddb["fpgas"]},
                {400000},
            )
            self.assertEqual(
                {link["data_lanes_per_direction"] for link in boarddb["links"]},
                {1},
            )
            self.assertEqual(
                {link["capacity_sharing"] for link in boarddb["links"]},
                {"shared_bidirectional"},
            )
            constraints_path = root / "rtl-boarddb.route_constraints.json"
            self.assertEqual(report["route_constraints"], str(constraints_path))
            constraints = read_json(constraints_path)
            self.assertEqual(
                constraints["schema"],
                "emuflow.system-route-constraints/v1",
            )
            self.assertEqual(constraints["frame_slots"], 512)
            self.assertEqual(constraints["tdm_ratio_quantum"], 8)
            self.assertEqual(
                set(constraints["shared_capacity_links"]),
                {link["id"] for link in boarddb["links"]},
            )

    def test_rtl_boarddb_materializer_requires_explicit_heterogeneous_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            template = root / "heterogeneous.json"
            template.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.boarddb/v1",
                        "platform": {"name": "heterogeneous", "kind": "virtual"},
                        "fpgas": [
                            {
                                "id": "small",
                                "part": "academic-small",
                                "utilization_limit": 0.75,
                                "capacity": {"lut": 100},
                            },
                            {
                                "id": "large",
                                "part": "academic-large",
                                "utilization_limit": 0.75,
                                "capacity": {"lut": 200},
                            },
                        ],
                        "links": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "heterogeneous"):
                materialize_eda2025_rtl_boarddb(
                    instance_path=normalized / "contest_instance.json",
                    device_template_path=template,
                    output_path=root / "bad.json",
                    name="bad",
                )
            report = materialize_eda2025_rtl_boarddb(
                instance_path=normalized / "contest_instance.json",
                device_template_path=template,
                output_path=root / "selected.json",
                name="selected",
                template_fpga_id="large",
                lane_scale=2,
            )
            self.assertEqual(report["template_fpga"], "large")
            self.assertEqual(report["data_lanes"], 8)

    def test_checker_rejects_route_on_disconnected_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            routes = {
                "schema": "emuflow.system-routes/v1",
                "routes": [
                    {
                        "id": "d000000",
                        "net": "net_000001",
                        "source": "F3",
                        "sinks": ["F1", "F4"],
                        "tree_edges": [
                            {"link": "invented", "from": "F3", "to": "F1"},
                            {"link": "x", "from": "F3", "to": "F4"},
                        ],
                    },
                    {
                        "id": "d000001",
                        "net": "net_000002",
                        "source": "F1",
                        "sinks": ["F2"],
                        "tree_edges": [
                            {"link": "x", "from": "F1", "to": "F2"}
                        ],
                    },
                    {
                        "id": "d000002",
                        "net": "net_000003",
                        "source": "F4",
                        "sinks": ["F3"],
                        "tree_edges": [
                            {"link": "x", "from": "F4", "to": "F3"}
                        ],
                    },
                ],
            }
            routes_path = root / "routes.json"
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "disconnected FPGA pair"):
                evaluate_eda2025_routes(
                    normalized / "contest_instance.json", routes_path
                )

    def test_import_rejects_non_unit_net_weight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            del normalized
            bad_net = root / "source" / "design.net"
            bad_net.write_text("g1 2 g2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unit net weights"):
                import_eda2025_instance(
                    info_path=root / "source" / "design.info",
                    net_path=bad_net,
                    topology_path=root / "source" / "design.topo",
                    assignment_path=root / "source" / "design.fpga.out",
                    output_dir=root / "bad",
                    name="bad",
                )

    def test_checker_enforces_published_topology_change_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            routed = root / "routed"
            run_phase4(
                assignment_path=normalized / "partition_assignment.json",
                platform_path=normalized / "boarddb.json",
                output_dir=routed,
                constraints_path=normalized / "route_constraints.json",
                timing_paths_path=normalized / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            changed = root / "changed.topo"
            changed.write_text(
                "F1: 0,0,1,1\n"
                "F2: 0,0,1,0\n"
                "F3: 1,1,0,1\n"
                "F4: 1,0,1,0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "topology changes"):
                evaluate_eda2025_routes(
                    normalized / "contest_instance.json",
                    routed / "routes.json",
                    new_topology_path=changed,
                )

    def test_cpp_topology_refinement_emits_rerouting_ready_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "design.info").write_text(INFO, encoding="utf-8")
            (source / "design.topo").write_text(TOPOLOGY, encoding="utf-8")
            nets = "".join(f"s{index} 1 t{index}\n" for index in range(9))
            (source / "design.net").write_text(nets, encoding="utf-8")
            (source / "design.fpga.out").write_text(
                "F1: " + " ".join(f"s{index}" for index in range(9)) + "\n"
                "F2: " + " ".join(f"t{index}" for index in range(9)) + "\n"
                "F3:\nF4:\n",
                encoding="utf-8",
            )
            normalized = root / "normalized"
            import_eda2025_instance(
                info_path=source / "design.info",
                net_path=source / "design.net",
                topology_path=source / "design.topo",
                assignment_path=source / "design.fpga.out",
                output_dir=normalized,
                name="topology_refinement",
            )
            initial_routed = root / "initial-routed"
            run_phase4(
                assignment_path=normalized / "partition_assignment.json",
                platform_path=normalized / "boarddb.json",
                output_dir=initial_routed,
                constraints_path=normalized / "route_constraints.json",
                timing_paths_path=normalized / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            optimized = root / "optimized"
            report = optimize_eda2025_topology(
                instance_path=normalized / "contest_instance.json",
                routes_path=initial_routed / "routes.json",
                output_dir=optimized,
                executable=str(eda2025_topology_optimizer()),
            )
            self.assertEqual(report["metrics"]["changed_channels"], 1)
            self.assertAlmostEqual(
                report["metrics"]["initial_worst_path_delay_ns"], 41.2
            )
            self.assertAlmostEqual(
                report["metrics"]["predicted_worst_path_delay_ns"], 35.6
            )
            rerouted = root / "rerouted"
            run_phase4(
                assignment_path=optimized / "normalized" / "partition_assignment.json",
                platform_path=optimized / "normalized" / "boarddb.json",
                output_dir=rerouted,
                constraints_path=optimized / "normalized" / "route_constraints.json",
                timing_paths_path=optimized / "normalized" / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            checked = evaluate_eda2025_routes(
                normalized / "contest_instance.json",
                rerouted / "routes.json",
                new_topology_path=optimized / "design.newtopo",
            )
            self.assertAlmostEqual(checked["metrics"]["worst_path_delay_ns"], 35.6)
            second = optimize_eda2025_topology(
                instance_path=normalized / "contest_instance.json",
                routes_path=rerouted / "routes.json",
                output_dir=root / "second",
                executable=str(eda2025_topology_optimizer()),
                current_topology_path=optimized / "design.newtopo",
            )
            self.assertEqual(second["metrics"]["input_changed_channels"], 1)
            self.assertEqual(second["metrics"]["iteration_changed_channels"], 0)
            self.assertEqual(second["metrics"]["changed_channels"], 1)

    def test_cpp_topology_refinement_swaps_zero_cost_donor_channels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "design.info").write_text(INFO, encoding="utf-8")
            (source / "design.topo").write_text(
                "F1: 0,1,0,2\n"
                "F2: 1,0,2,0\n"
                "F3: 0,2,0,1\n"
                "F4: 2,0,1,0\n",
                encoding="utf-8",
            )
            (source / "design.net").write_text(
                "".join(f"s{index} 1 t{index}\n" for index in range(9)),
                encoding="utf-8",
            )
            (source / "design.fpga.out").write_text(
                "F1: " + " ".join(f"s{index}" for index in range(9)) + "\n"
                "F2: " + " ".join(f"t{index}" for index in range(9)) + "\n"
                "F3:\nF4:\n",
                encoding="utf-8",
            )
            normalized = root / "normalized"
            import_eda2025_instance(
                info_path=source / "design.info",
                net_path=source / "design.net",
                topology_path=source / "design.topo",
                assignment_path=source / "design.fpga.out",
                output_dir=normalized,
                name="channel_swap",
                topology_change_fraction=1.0,
            )
            routed = root / "routed"
            run_phase4(
                assignment_path=normalized / "partition_assignment.json",
                platform_path=normalized / "boarddb.json",
                output_dir=routed,
                constraints_path=normalized / "route_constraints.json",
                timing_paths_path=normalized / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            report = optimize_eda2025_topology(
                instance_path=normalized / "contest_instance.json",
                routes_path=routed / "routes.json",
                output_dir=root / "optimized",
                executable=str(eda2025_topology_optimizer()),
            )
            self.assertEqual(report["metrics"]["iteration_changed_channels"], 3)
            changes = {tuple(change["fpgas"]): change for change in report["changes"]}
            self.assertEqual(changes[("F1", "F2")]["optimized_channels"], 2)
            self.assertEqual(changes[("F1", "F4")]["optimized_channels"], 1)
            self.assertEqual(changes[("F2", "F3")]["optimized_channels"], 1)

    def test_normalized_artifacts_are_self_describing(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, normalized = self._import(Path(temporary))
            instance = read_json(normalized / "contest_instance.json")
            boarddb = read_json(normalized / "boarddb.json")
            constraints = read_json(normalized / "route_constraints.json")
            timing_paths = read_json(
                normalized / "contest_timing_paths.json"
            )
            self.assertEqual(
                instance["source"]["contest"], "2025 EDA Elite Challenge"
            )
            self.assertEqual(boarddb["platform"]["kind"], "virtual")
            self.assertEqual(constraints["tdm_ratio_quantum"], 8)
            self.assertEqual(constraints["frame_slots"], 512)
            self.assertEqual(
                len(constraints["shared_capacity_links"]), 4
            )
            self.assertEqual(
                set(constraints["link_delay_ns"].values()), {30.7}
            )
            self.assertEqual(len(timing_paths["paths"]), 4)


if __name__ == "__main__":
    unittest.main()
