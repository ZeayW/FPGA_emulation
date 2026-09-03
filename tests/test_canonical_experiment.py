import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from emuflow.canonical_experiment import (
    CANONICAL_EXPERIMENT_CONFIG_SCHEMA,
    compile_canonical_experiment_spec,
    compile_static_exact_ab_experiment_spec,
)
from emuflow.experiment_dag import validate_experiment_spec
from emuflow.errors import ValidationError
from emuflow.experiment_identity import build_implementation_closure
from emuflow.io import write_json
from emuflow.tdm_ratio import TDM_TIMING_DAG_RATIO_PROVIDER
from emuflow.timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    NATIVE_TIMING_EVALUATED_PROVIDER,
)


REPOSITORY = Path(__file__).resolve().parents[1]


class CanonicalExperimentTest(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        openparf_install = root / "openparf-install"
        (openparf_install / "openparf").mkdir(parents=True, exist_ok=True)
        (openparf_install / "openparf.py").write_text(
            "# fixture loader\n", encoding="utf-8"
        )
        (openparf_install / "openparf/__init__.py").write_text(
            "# fixture package\n", encoding="utf-8"
        )
        manifest = root / "openparf-manifest.json"
        write_json(
            manifest,
            build_implementation_closure(
                openparf_install, ["openparf.py", "openparf"]
            ),
        )
        tool_names = (
            "emuflow",
            "yosys",
            "opensta",
            "openroad",
            "hop_refiner",
            "patron_refiner",
            "router",
            "ratio_optimizer",
            "timing_dag_optimizer",
            "slot_optimizer",
            "pin_planner",
            "chimew_grouper",
            "chimew_refiner",
            "chimew_rudy",
            "chimew_assigner",
            "vpr",
            "architecture_importer",
            "packed_importer",
            "route_checker",
            "openparf_python",
        )
        rtl = root / "dla_like.medium.v"
        rtl.write_text("module DLA(input clk); endmodule\n", encoding="utf-8")
        platform = root / "boarddb.json"
        platform_value = json.loads(
            (REPOSITORY / "platforms/virtual/xcvu3p_2fpga_p2p.json").read_text()
        )
        platform_value["platform"]["name"] = "eda2023-case6-rtl"
        platform.write_text(json.dumps(platform_value), encoding="utf-8")
        route_constraints = root / "route_constraints.json"
        route_constraints.write_text(
            json.dumps(
                {
                    "schema": "emuflow.system-route-constraints/v1",
                    "frame_slots": 32,
                    "max_route_hops": 2,
                    "tdm_ratio_quantum": 4,
                    "tdm_min_ratio": 4,
                }
            ),
            encoding="utf-8",
        )
        from emuflow.contest_validation_matrix import load_contest_validation_matrix
        import hashlib

        _, contest_validation = load_contest_validation_matrix(
            REPOSITORY / "benchmarks/contest_validation_matrix.json"
        )
        boarddb_report = root / "boarddb_report.json"
        boarddb_report.write_text(
            json.dumps(
                {
                    "schema": "emuflow.public-contest-boarddb-report/v1",
                    "status": "pass",
                    "case_id": "eda2023.case6",
                    "suite": "eda2023",
                    "gate": "materialize-boarddb",
                    "matrix_sha256": contest_validation["matrix_sha256"],
                    "qualification": "academic-architecture-projection",
                    "projection": {},
                    "adapter": {},
                    "artifacts": [
                        {
                            "path": "boarddb.json",
                            "bytes": platform.stat().st_size,
                            "sha256": hashlib.sha256(platform.read_bytes()).hexdigest(),
                        },
                        {
                            "path": "route_constraints.json",
                            "bytes": route_constraints.stat().st_size,
                            "sha256": hashlib.sha256(
                                route_constraints.read_bytes()
                            ).hexdigest(),
                        },
                    ],
                    "phase3_status": "not-run",
                }
            ),
            encoding="utf-8",
        )
        value = {
            "schema": CANONICAL_EXPERIMENT_CONFIG_SCHEMA,
            "case_id": "koios-dla-medium-l5__eda2023-case6",
            "source_commit": "a" * 40,
            "rtl_source": str(rtl),
            "platform": str(platform),
            "boarddb_report": str(boarddb_report),
            "route_constraints": str(route_constraints),
            "timing_model": str(
                REPOSITORY / "resources/timing/ultrascaleplus-softlogic-v1.json"
            ),
            "architecture_timing_db": str(
                REPOSITORY
                / "resources/architectures/vtr/flagship-k6-n10-40nm.json"
            ),
            "physical_architecture": str(
                REPOSITORY / "examples/architecture/vtr_k6_heterogeneous_fixture.xml"
            ),
            "tools": {name: sys.executable for name in tool_names},
            "openparf_install": str(openparf_install),
            "openparf_manifest": str(manifest),
            "top": "DLA",
            "clocks": ["clk"],
            "clock_periods": {"clk": 10.0},
            "partition_seed_attempts": 6,
            "partition_repair_balance": True,
            "physical_workers": 8,
        }
        path = root / "config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_compiler_defaults_to_one_physical_seed_per_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "spec.json"
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_provider"] = "tritonpart"
            config["cut_mode"] = "sequential-only"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            report = compile_canonical_experiment_spec(
                config_path, REPOSITORY, output
            )
            self.assertEqual(report["nodes"], 15)
            self.assertEqual(report["physical_terminal_nodes"], 3)
            self.assertEqual(report["terminal_nodes"], 1)
            spec = validate_experiment_spec(json.loads(output.read_text()))
            nodes = {item["id"]: item for item in spec["nodes"]}
            self.assertEqual(
                [
                    item["id"]
                    for item in spec["nodes"][:7]
                ],
                [
                    "frontend",
                    "timing",
                    "partition",
                    "cut-timing",
                    "route",
                    "tdm",
                    "shared-phase1-5",
                ],
            )
            self.assertEqual(nodes["route"]["dependencies"], ["partition", "cut-timing"])
            self.assertIn(
                "{dependency:timing}", nodes["cut-timing"]["validator"]
            )
            self.assertIn("--opensta", nodes["timing"]["command"])
            self.assertNotIn("--opensta", nodes["cut-timing"]["command"])
            self.assertEqual(nodes["tdm"]["dependencies"], ["route"])
            self.assertIn("--route-constraints", nodes["partition"]["command"])
            self.assertEqual(
                nodes["frontend"]["configuration"]["mapping_profile"],
                "vtr-hard-blocks",
            )
            self.assertEqual(
                nodes["frontend"]["command_identity"][0],
                "{input:tool.emuflow}",
            )
            self.assertIn(
                "{input:rtl}", nodes["frontend"]["command_identity"]
            )
            self.assertNotIn(
                "src/emuflow/experiment_partition.py",
                nodes["frontend"]["implementation"]["components"],
            )
            self.assertIn(
                "src/emuflow/experiment_upstream.py::run_frontend_checkpoint,validate_frontend_checkpoint",
                nodes["frontend"]["implementation"]["components"],
            )
            self.assertNotIn(
                "src/emuflow/experiment_upstream.py",
                nodes["frontend"]["implementation"]["components"],
            )
            self.assertIn(
                "src/emuflow/experiment_partition.py",
                nodes["partition"]["implementation"]["components"],
            )
            self.assertIn(
                "src/emuflow/chimew_refinement.py",
                nodes["physical-lookahead"]["implementation"]["components"],
            )
            self.assertIn(
                "src/native/chimew_position_refiner.cpp",
                nodes["physical-lookahead"]["implementation"]["components"],
            )
            self.assertIn("--hop-refiner", nodes["partition"]["command"])
            self.assertEqual(
                nodes["partition"]["configuration"]["seed_attempts"], 6
            )
            self.assertTrue(
                nodes["partition"]["configuration"]["repair_balance"]
            )
            self.assertEqual(
                nodes["partition"]["command"][
                    nodes["partition"]["command"].index("--seed-attempts") + 1
                ],
                "6",
            )
            self.assertIn("--repair-balance", nodes["partition"]["command"])
            self.assertIn(
                "--managed-dag-node", nodes["partition"]["command"]
            )
            self.assertIn(
                "--online-validation", nodes["partition"]["validator"]
            )
            self.assertEqual(
                nodes["partition"]["validator"][
                    nodes["partition"]["validator"].index("--seed-attempts") + 1
                ],
                "6",
            )
            self.assertIn("--repair-balance", nodes["partition"]["validator"])
            self.assertIn("--constraints", nodes["route"]["command"])
            self.assertEqual(
                nodes["route"]["configuration"]["provider"],
                GLOBAL_CANDIDATE_PROVIDER,
            )
            self.assertEqual(nodes["route"]["configuration"]["candidate_workers"], 8)
            self.assertEqual(
                nodes["route"]["command"][
                    nodes["route"]["command"].index("--provider") + 1
                ],
                GLOBAL_CANDIDATE_PROVIDER,
            )
            self.assertEqual(
                nodes["route"]["validator"][
                    nodes["route"]["validator"].index("--candidate-workers") + 1
                ],
                "8",
            )
            self.assertEqual(
                nodes["tdm"]["configuration"]["provider"],
                TDM_TIMING_DAG_RATIO_PROVIDER,
            )
            self.assertEqual(
                nodes["tdm"]["command"][
                    nodes["tdm"]["command"].index("--provider") + 1
                ],
                TDM_TIMING_DAG_RATIO_PROVIDER,
            )
            self.assertEqual(
                nodes["tdm"]["validator"][
                    nodes["tdm"]["validator"].index("--provider") + 1
                ],
                TDM_TIMING_DAG_RATIO_PROVIDER,
            )
            self.assertEqual(nodes["tdm"]["configuration"]["ratio_quantum"], 4)
            self.assertEqual(nodes["tdm"]["configuration"]["max_ratio"], 32)
            self.assertIn(
                "--pin-planner", nodes["phase6-placement-aware"]["command"]
            )
            for argument in (
                "--chimew-grouper",
                "--chimew-refiner",
                "--chimew-rudy",
                "--chimew-assigner",
            ):
                self.assertIn(argument, nodes["phase6-chimew"]["command"])
            self.assertIn(
                "--reuse-validated-phase6-equivalence",
                nodes["physical-lookahead"]["command"],
            )
            self.assertIn(
                "--reuse-validated-phase6-equivalence",
                nodes["physical-lookahead"]["validator"],
            )
            terminals = [item for item in spec["nodes"] if item["stage"] == "phase7"]
            self.assertEqual(
                {(item["provider"], item["physical_seed"]) for item in terminals},
                {(provider, 1) for provider in ("baseline", "placement-aware", "chimew")},
            )
            self.assertTrue(all(item["configuration"]["physical_workers"] == 8 for item in terminals))
            for terminal in terminals:
                self.assertIn(
                    "--reuse-validated-phase6-equivalence",
                    terminal["command"],
                )
                self.assertIn(
                    "--reuse-validated-phase6-equivalence",
                    terminal["validator"],
                )
                roles = {
                    item["path"]: item["role"] for item in terminal["artifacts"]
                }
                self.assertEqual(
                    roles["physical/physical-summary.json"],
                    "evidence-critical",
                )
                self.assertEqual(
                    roles["physical/multi-fpga-physical-flow-report.json"],
                    "evidence-critical",
                )
                self.assertNotIn("physical", roles)
            self.assertTrue(
                all(
                    item["validator"][-7:]
                    == [
                        "--seed",
                        str(item["physical_seed"]),
                        "--workers",
                        "8",
                        "--route-channel-width",
                        "300",
                        "--managed-dag-node",
                    ]
                    for item in terminals
                )
            )

    def test_compiler_defaults_to_generalized_static_exact_patron(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "spec.json"
            report = compile_canonical_experiment_spec(
                self._config(root), REPOSITORY, output
            )
            nodes = {
                node["id"]: node
                for node in validate_experiment_spec(
                    json.loads(output.read_text())
                )["nodes"]
            }
            partition = nodes["partition"]
            self.assertEqual(report["cut_mode"], "static-exact-combinational")
            self.assertEqual(
                partition["configuration"]["provider"], "patron"
            )
            self.assertEqual(
                partition["configuration"]["patron_algorithm_version"], 6
            )
            self.assertEqual(
                partition["configuration"]["cut_mode"],
                "static-exact-combinational",
            )
            self.assertEqual(
                partition["configuration"]["static_exact_candidate_policy"],
                "assignment-derived-acyclic-v2",
            )
            self.assertEqual(
                partition["configuration"][
                    "max_cross_fpga_dependency_depth"
                ],
                8,
            )
            self.assertFalse(
                partition["configuration"]["mfspart_post_refinement"]
            )
            self.assertIn("--patron-refiner", partition["command"])
            self.assertIn(
                "--patron-initial-assignment", partition["command"]
            )
            self.assertIn(
                "--patron-initial-clusters", partition["command"]
            )
            self.assertEqual(
                partition["dependencies"],
                ["frontend", "timing", "patron-initial-partition"],
            )
            baseline = nodes["patron-initial-partition"]
            self.assertEqual(
                baseline["configuration"]["provider"], "tritonpart"
            )
            self.assertEqual(
                baseline["configuration"]["purpose"],
                "shared-patron-initial-assignment",
            )
            self.assertNotIn("--patron-refiner", baseline["command"])
            self.assertIn(
                "--no-mfspart-post-refinement", baseline["command"]
            )
            self.assertIn(
                "--no-mfspart-post-refinement", baseline["validator"]
            )
            self.assertEqual(
                partition["configuration"][
                    "patron_initial_assignment_source"
                ],
                "patron-initial-partition",
            )
            self.assertEqual(
                partition["configuration"][
                    "patron_initial_clusters_source"
                ],
                "patron-initial-partition",
            )
            self.assertFalse(
                any(
                    artifact["path"] == "patron"
                    for artifact in partition["artifacts"]
                )
            )
            tdm = nodes["tdm"]
            self.assertEqual(
                tdm["configuration"]["slot_refinement_iterations"], 200
            )
            self.assertEqual(
                tdm["command"][
                    tdm["command"].index("--slot-refinement-iterations") + 1
                ],
                "200",
            )

    def test_compiler_can_reuse_a_frozen_baseline_for_patron(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            initial = root / "baseline-assignment.json"
            initial.write_text('{"frozen":"fixture"}\n', encoding="utf-8")
            initial_clusters = root / "baseline-clusters.json"
            initial_clusters.write_text(
                '{"schema":"emuflow.clusters/v1"}\n', encoding="utf-8"
            )
            physical_timing = root / "prior-system-timing.json"
            physical_timing.write_text(
                '{"schema":"emuflow.system-timing/v2"}\n',
                encoding="utf-8",
            )
            config["partition_provider"] = "patron"
            config["cut_mode"] = "sequential-only"
            config["patron_flow_refinement"] = True
            config["patron_initial_assignment"] = str(initial)
            config["patron_initial_clusters"] = str(initial_clusters)
            config["patron_physical_system_timing"] = str(physical_timing)
            config["patron_physical_feedback_scale"] = 0.25
            config["tools"]["patron_refiner"] = sys.executable
            config["phase6_providers"] = ["chimew"]
            config["physical_seeds"] = [1]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "patron-spec.json"
            report = compile_canonical_experiment_spec(
                config_path, REPOSITORY, output
            )
            spec = validate_experiment_spec(json.loads(output.read_text()))
            nodes = {node["id"]: node for node in spec["nodes"]}
            partition = nodes["partition"]
            self.assertEqual(partition["configuration"]["provider"], "patron")
            self.assertTrue(
                partition["configuration"]["patron_flow_refinement"]
            )
            self.assertEqual(
                partition["configuration"]["patron_algorithm_version"], 11
            )
            self.assertIn("--patron-refiner", partition["command"])
            self.assertIn(
                "--patron-initial-clusters", partition["command"]
            )
            self.assertIn("--patron-flow-refinement", partition["command"])
            self.assertIn(
                "--patron-flow-refinement", partition["validator"]
            )
            self.assertIn(
                "--patron-initial-assignment", partition["command"]
            )
            self.assertIn(
                "--patron-initial-assignment", partition["validator"]
            )
            self.assertIn(
                "--patron-physical-system-timing", partition["command"]
            )
            self.assertIn(
                "--patron-physical-system-timing", partition["validator"]
            )
            self.assertEqual(
                partition["configuration"][
                    "patron_physical_feedback_scale"
                ],
                0.25,
            )
            self.assertIn(
                "patron_physical_system_timing", partition["inputs"]
            )
            self.assertIn(
                "src/emuflow/partition_pressure.py",
                partition["implementation"]["components"],
            )
            self.assertIn(
                "src/emuflow/partition_physical_feedback.py",
                partition["implementation"]["components"],
            )
            self.assertIn(
                "src/native/patron_refiner.cpp",
                partition["implementation"]["components"],
            )
            self.assertFalse(
                any(
                    artifact["path"] == "patron"
                    for artifact in partition["artifacts"]
                )
            )
            self.assertNotIn("patron-initial-partition", nodes)
            terminals = [
                item for item in spec["nodes"] if item["stage"] == "phase7"
            ]
            self.assertEqual(
                {
                    (item["provider"], item["physical_seed"])
                    for item in terminals
                },
                {("chimew", 1)},
            )
            self.assertNotIn("qor-comparison", nodes)
            self.assertEqual(report["physical_terminal_nodes"], 1)
            self.assertEqual(report["terminal_nodes"], 1)

    def test_compiler_selects_exact_patron_v9(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["patron_algorithm_version"] = 9
            config["patron_flow_refinement"] = True
            config["patron_max_moves"] = 16
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "patron-v9-spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            spec = validate_experiment_spec(json.loads(output.read_text()))
            partition = {node["id"]: node for node in spec["nodes"]}[
                "partition"
            ]
            self.assertEqual(
                partition["configuration"]["patron_algorithm_version"], 9
            )
            self.assertEqual(
                partition["configuration"]["patron_max_moves"], 16
            )
            for command in (partition["command"], partition["validator"]):
                self.assertEqual(
                    command[command.index("--patron-max-moves") + 1],
                    "16",
                )
            self.assertEqual(
                partition["command"][
                    partition["command"].index(
                        "--patron-algorithm-version"
                    )
                    + 1
                ],
                "9",
            )
            config["cut_mode"] = "static-exact-combinational"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            exact_output = root / "patron-exact.json"
            compile_canonical_experiment_spec(
                config_path, REPOSITORY, exact_output
            )
            exact_nodes = {
                node["id"]: node
                for node in validate_experiment_spec(
                    json.loads(exact_output.read_text())
                )["nodes"]
            }
            self.assertEqual(
                exact_nodes["partition"]["configuration"]["cut_mode"],
                "static-exact-combinational",
            )
            config["patron_algorithm_version"] = 12
            config_path.write_text(json.dumps(config), encoding="utf-8")
            guarded_output = root / "patron-v12-static-exact.json"
            compile_canonical_experiment_spec(
                config_path, REPOSITORY, guarded_output
            )
            guarded_nodes = {
                node["id"]: node
                for node in validate_experiment_spec(
                    json.loads(guarded_output.read_text())
                )["nodes"]
            }
            self.assertEqual(
                guarded_nodes["partition"]["configuration"][
                    "patron_algorithm_version"
                ],
                12,
            )
            config["patron_algorithm_version"] = 13
            config_path.write_text(json.dumps(config), encoding="utf-8")
            transition_guarded_output = (
                root / "patron-v13-static-exact.json"
            )
            compile_canonical_experiment_spec(
                config_path, REPOSITORY, transition_guarded_output
            )
            transition_guarded_nodes = {
                node["id"]: node
                for node in validate_experiment_spec(
                    json.loads(transition_guarded_output.read_text())
                )["nodes"]
            }
            self.assertEqual(
                transition_guarded_nodes["partition"]["configuration"][
                    "patron_algorithm_version"
                ],
                13,
            )
            config["cut_mode"] = "sequential-only"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "v12/v13/v14 requires generalized Static Exact"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "invalid-v12.json"
                )

    def test_patron_max_moves_is_positive_and_patron_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["patron_max_moves"] = 0
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "patron_max_moves"):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "zero.json"
                )

            config["patron_max_moves"] = 16
            config["partition_provider"] = "tritonpart"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "requires partition_provider 'patron'"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "tritonpart.json"
                )

    def test_physical_storage_peak_override_is_sealed_and_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_provider"] = "tritonpart"
            config["cut_mode"] = "sequential-only"
            config["physical_peak_gib"] = 12
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item
                for item in validate_experiment_spec(json.loads(output.read_text()))[
                    "nodes"
                ]
            }
            physical = [
                nodes["physical-lookahead"],
                *(item for item in nodes.values() if item["stage"] == "phase7"),
            ]
            self.assertTrue(
                all(
                    item["configuration"]["physical_peak_gib"] == 12
                    and item["storage_estimate"]["peak_bytes"] == 12 * 1024**3
                    for item in physical
                )
            )

            config["physical_peak_gib"] = 0
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "physical_peak_gib"):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "invalid-spec.json"
                )

            comparison = nodes["qor-comparison"]
            self.assertEqual(
                comparison["dependencies"],
                [
                    "shared-phase1-5",
                    *[
                        f"phase7-{provider}-seed{seed}"
                        for provider in ("baseline", "placement-aware", "chimew")
                        for seed in (1,)
                    ],
                ],
            )
            self.assertEqual(
                comparison["artifacts"],
                [
                    {
                        "path": "canonical-qor-comparison.json",
                        "role": "evidence-critical",
                        "retention": "required",
                    }
                ],
            )

    def test_partition_storage_estimate_override_is_sealed_and_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_peak_gib"] = 4
            config["partition_retained_gib"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item
                for item in validate_experiment_spec(json.loads(output.read_text()))[
                    "nodes"
                ]
            }
            partition = nodes["partition"]
            self.assertEqual(partition["configuration"]["partition_peak_gib"], 4)
            self.assertEqual(
                partition["configuration"]["partition_retained_gib"], 1
            )
            self.assertEqual(
                partition["storage_estimate"],
                {
                    "peak_bytes": 4 * 1024**3,
                    "retained_bytes": 1 * 1024**3,
                },
            )

            for field in ("partition_peak_gib", "partition_retained_gib"):
                invalid = dict(config)
                invalid[field] = 0
                config_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, field):
                    compile_canonical_experiment_spec(
                        config_path,
                        REPOSITORY,
                        root / f"invalid-{field}.json",
                    )

    def test_phase6_candidate_storage_peak_override_is_sealed_and_positive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_provider"] = "tritonpart"
            config["cut_mode"] = "sequential-only"
            config["phase6_candidate_peak_gib"] = 4
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item
                for item in validate_experiment_spec(json.loads(output.read_text()))[
                    "nodes"
                ]
            }
            for provider in ("placement-aware", "chimew"):
                node = nodes[f"phase6-{provider}"]
                self.assertEqual(
                    node["configuration"]["phase6_candidate_peak_gib"], 4
                )
                self.assertEqual(
                    node["storage_estimate"]["peak_bytes"], 4 * 1024**3
                )
            self.assertNotIn(
                "phase6_candidate_peak_gib",
                nodes["phase6-baseline"]["configuration"],
            )
            self.assertEqual(
                nodes["phase6-baseline"]["storage_estimate"]["peak_bytes"],
                12 * 1024**3,
            )

            config["phase6_candidate_peak_gib"] = 0
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "phase6_candidate_peak_gib"
            ):
                compile_canonical_experiment_spec(
                    config_path,
                    REPOSITORY,
                    root / "invalid-phase6-candidate-spec.json",
                )

    def test_external_tool_bytes_are_part_of_execution_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            first = root / "first.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, first)
            config = json.loads(config_path.read_text())
            replacement = root / "replacement-tool"
            replacement.write_text("different tool bytes\n", encoding="utf-8")
            replacement.chmod(0o755)
            config["tools"]["router"] = str(replacement)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            second = root / "second.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, second)
            first_nodes = {item["id"]: item for item in json.loads(first.read_text())["nodes"]}
            second_nodes = {item["id"]: item for item in json.loads(second.read_text())["nodes"]}
            self.assertNotEqual(
                first_nodes["route"]["inputs"]["tool.router"],
                second_nodes["route"]["inputs"]["tool.router"],
            )
            self.assertEqual(
                first_nodes["frontend"]["inputs"], second_nodes["frontend"]["inputs"]
            )

    def test_route_candidate_workers_are_explicitly_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_provider"] = "tritonpart"
            config["cut_mode"] = "sequential-only"
            config["route_candidate_workers"] = 3
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item for item in json.loads(output.read_text())["nodes"]
            }
            route = nodes["route"]
            self.assertEqual(route["configuration"]["candidate_workers"], 3)
            self.assertEqual(
                route["command"][route["command"].index("--candidate-workers") + 1],
                "3",
            )
            self.assertEqual(
                route["validator"][
                    route["validator"].index("--candidate-workers") + 1
                ],
                "3",
            )

    def test_static_exact_mode_defaults_to_one_physical_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config.update(
                {
                    "cut_mode": "static-exact-combinational",
                    "partition_provider": "tritonpart",
                    "max_cross_fpga_dependency_depth": 2,
                    "route_candidate_workers": 7,
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            report = compile_canonical_experiment_spec(
                config_path, REPOSITORY, output
            )
            self.assertEqual(report["physical_terminal_nodes"], 3)
            self.assertEqual(report["terminal_nodes"], 1)
            self.assertEqual(
                report["cut_mode"], "static-exact-combinational"
            )
            nodes = {
                item["id"]: item
                for item in validate_experiment_spec(
                    json.loads(output.read_text())
                )["nodes"]
            }
            self.assertIn("phase6-placement-aware", nodes)
            self.assertIn("phase6-chimew", nodes)
            self.assertIn("qor-comparison", nodes)
            partition = nodes["partition"]
            self.assertTrue(
                partition["configuration"]["mfspart_post_refinement"]
            )
            self.assertEqual(
                partition["configuration"][
                    "mfspart_post_refinement_bottleneck_beta"
                ],
                256.0,
            )
            self.assertIn("--mfspart-post-refinement", partition["command"])
            self.assertEqual(
                partition["command"][
                    partition["command"].index(
                        "--mfspart-post-refinement-bottleneck-beta"
                    )
                    + 1
                ],
                "256",
            )
            self.assertIn(
                "mfspart-post-refinement",
                {artifact["path"] for artifact in partition["artifacts"]},
            )
            self.assertEqual(
                partition["configuration"]["cut_mode"],
                "static-exact-combinational",
            )
            self.assertEqual(
                partition["configuration"][
                    "minimum_combinational_cut_nets"
                ],
                0,
            )
            self.assertEqual(
                partition["configuration"][
                    "max_cross_fpga_dependency_depth"
                ],
                2,
            )
            self.assertIn("--cut-mode", partition["command"])
            self.assertIn("--cut-mode", partition["validator"])
            self.assertEqual(
                partition["command"][
                    partition["command"].index(
                        "--minimum-combinational-cut-nets"
                    )
                    + 1
                ],
                "0",
            )
            self.assertEqual(
                partition["validator"][
                    partition["validator"].index(
                        "--minimum-combinational-cut-nets"
                    )
                    + 1
                ],
                "0",
            )
            route = nodes["route"]

    def test_static_exact_omitted_knobs_select_promoted_generalized_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["cut_mode"] = "static-exact-combinational"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item
                for item in validate_experiment_spec(
                    json.loads(output.read_text())
                )["nodes"]
            }
            partition = nodes["partition"]
            self.assertEqual(
                partition["configuration"]["static_exact_candidate_policy"],
                "assignment-derived-acyclic-v2",
            )
            self.assertEqual(
                partition["configuration"][
                    "max_cross_fpga_dependency_depth"
                ],
                8,
            )
            self.assertEqual(
                partition["configuration"][
                    "mfspart_post_refinement_timing_path_beta"
                ],
                0.0,
            )

    def test_generalized_static_exact_accepts_depth_beyond_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config.update(
                {
                    "cut_mode": "static-exact-combinational",
                    "static_exact_candidate_policy": (
                        "assignment-derived-acyclic-v2"
                    ),
                    "max_cross_fpga_dependency_depth": 8,
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item
                for item in validate_experiment_spec(
                    json.loads(output.read_text())
                )["nodes"]
            }
            partition = nodes["partition"]
            self.assertEqual(
                partition["configuration"]["static_exact_candidate_policy"],
                "assignment-derived-acyclic-v2",
            )
            for arguments in (partition["command"], partition["validator"]):
                index = arguments.index("--static-exact-candidate-policy")
                self.assertEqual(
                    arguments[index + 1], "assignment-derived-acyclic-v2"
                )
            route = nodes["route"]
            self.assertEqual(
                route["configuration"]["provider"],
                GLOBAL_CANDIDATE_PROVIDER,
            )
            self.assertEqual(route["configuration"]["candidate_workers"], 8)
            tdm = nodes["tdm"]
            self.assertEqual(
                tdm["configuration"]["provider"],
                TDM_TIMING_DAG_RATIO_PROVIDER,
            )
            self.assertIn("--ratio-optimizer", tdm["command"])
            self.assertIn(
                "ratio_plan.json",
                {item["path"] for item in tdm["artifacts"]},
            )
            terminals = [
                node for node in nodes.values() if node["stage"] == "phase7"
            ]
            self.assertEqual(
                {(item["provider"], item["physical_seed"]) for item in terminals},
                {
                    ("baseline", 1),
                    ("placement-aware", 1),
                    ("chimew", 1),
                },
            )

    def test_multiple_physical_seeds_remain_an_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_provider"] = "tritonpart"
            config["cut_mode"] = "sequential-only"
            config["physical_seeds"] = [1, 2, 3]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            report = compile_canonical_experiment_spec(
                config_path, REPOSITORY, output
            )
            self.assertEqual(report["physical_terminal_nodes"], 9)
            spec = validate_experiment_spec(json.loads(output.read_text()))
            self.assertEqual(
                {
                    (item["provider"], item["physical_seed"])
                    for item in spec["nodes"]
                    if item["stage"] == "phase7"
                },
                {
                    (provider, seed)
                    for provider in ("baseline", "placement-aware", "chimew")
                    for seed in (1, 2, 3)
                },
            )

    def test_physical_seed_list_must_be_sorted_unique_and_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            for invalid in ([], [2, 1], [1, 1], [0], [True]):
                config["physical_seeds"] = invalid
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(ValidationError, "physical_seeds"):
                    compile_canonical_experiment_spec(
                        config_path, REPOSITORY, root / "spec.json"
                    )

    def test_static_exact_mode_rejects_multiple_virtual_clocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config.update(
                {
                    "cut_mode": "static-exact-combinational",
                    "clocks": ["clk", "aux_clk"],
                    "clock_periods": {"clk": 10.0, "aux_clk": 10.0},
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "exactly one virtual DUT clock"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "spec.json"
                )

    def test_static_exact_can_optionally_require_an_actual_combinational_cut(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["cut_mode"] = "static-exact-combinational"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            report = compile_canonical_experiment_spec(
                config_path, REPOSITORY, root / "spec.json"
            )
            self.assertEqual(report["cut_mode"], "static-exact-combinational")
            nodes = {
                item["id"]: item
                for item in json.loads((root / "spec.json").read_text())["nodes"]
            }
            self.assertEqual(
                nodes["partition"]["configuration"]["minimum_combinational_cut_nets"],
                0,
            )

            config["minimum_combinational_cut_nets"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            compile_canonical_experiment_spec(
                config_path, REPOSITORY, root / "exercise-spec.json"
            )

            config["cut_mode"] = "sequential-only"
            config["minimum_combinational_cut_nets"] = 1
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "minimum_combinational_cut_nets"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "safe-spec.json"
                )

    def test_partition_constraints_are_sealed_for_run_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            constraints = root / "partition-constraints.json"
            constraints.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.partition-constraints/v1",
                        "fixed": [{"instance": "u0", "fpga": "FPGA0"}],
                    }
                ),
                encoding="utf-8",
            )
            config = json.loads(config_path.read_text())
            config["partition_constraints"] = str(constraints)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            nodes = {
                item["id"]: item
                for item in json.loads(output.read_text())["nodes"]
            }
            partition = nodes["partition"]
            self.assertIn("partition_constraints", partition["inputs"])
            self.assertEqual(
                partition["configuration"]["partition_constraints_sha256"],
                hashlib.sha256(constraints.read_bytes()).hexdigest(),
            )
            for command in (partition["command"], partition["validator"]):
                self.assertEqual(
                    command[command.index("--constraints") + 1],
                    str(constraints.resolve()),
                )

    def test_partition_seed_attempts_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_seed_attempts"] = 0
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "partition_seed_attempts"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "spec.json"
                )

    def test_precomputed_tritonpart_solution_is_sealed_for_single_arm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            solution = root / "candidate.part.3"
            solution.write_text("0\n1\n0\n", encoding="utf-8")
            config = json.loads(config_path.read_text())
            config["partition_seed_attempts"] = 1
            config["tritonpart_solution"] = str(solution)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(config_path, REPOSITORY, output)
            partition = next(
                item
                for item in json.loads(output.read_text())["nodes"]
                if item["id"] == "partition"
            )
            digest = hashlib.sha256(solution.read_bytes()).hexdigest()
            self.assertEqual(partition["inputs"]["tritonpart_solution"], digest)
            self.assertEqual(
                partition["configuration"]["tritonpart_solution_sha256"],
                digest,
            )
            for command in (partition["command"], partition["validator"]):
                self.assertEqual(
                    command[command.index("--tritonpart-solution") + 1],
                    str(solution.resolve()),
                )

            config["partition_seed_attempts"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "requires partition_seed_attempts=1"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "invalid.json"
                )

    def test_static_exact_ab_rejects_one_solution_for_different_clusterings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            solution = root / "candidate.part.3"
            solution.write_text("0\n", encoding="utf-8")
            config = json.loads(config_path.read_text())
            config["tritonpart_solution"] = str(solution)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "policy-specific partition searches"
            ):
                compile_static_exact_ab_experiment_spec(
                    config_path, REPOSITORY, root / "ab.json"
                )

    def test_partition_balance_repair_must_be_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_repair_balance"] = "true"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "partition_repair_balance"
            ):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "spec.json"
                )

    def test_disabled_partition_repair_is_explicitly_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_repair_balance"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            compile_canonical_experiment_spec(
                config_path, REPOSITORY, output
            )
            partition = next(
                item
                for item in json.loads(output.read_text())["nodes"]
                if item["id"] == "partition"
            )
            self.assertNotIn("--repair-balance", partition["command"])
            self.assertIn("--no-repair-balance", partition["validator"])

    def test_static_exact_ab_compiler_shares_only_frontend_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "static-exact-ab.json"
            report = compile_static_exact_ab_experiment_spec(
                self._config(root), REPOSITORY, output
            )
            self.assertEqual(report["nodes"], 21)
            self.assertEqual(report["physical_terminal_nodes"], 2)
            self.assertEqual(report["physical_seeds"], [1])
            spec = validate_experiment_spec(json.loads(output.read_text()))
            nodes = {item["id"]: item for item in spec["nodes"]}
            self.assertEqual(
                sum(item["stage"] == "frontend" for item in spec["nodes"]), 1
            )
            self.assertEqual(
                sum(item["stage"] == "timing" for item in spec["nodes"]), 1
            )
            self.assertNotIn("seq-phase6-placement-aware", nodes)
            self.assertNotIn("seq-phase6-chimew", nodes)
            self.assertEqual(
                nodes["seq-partition"]["configuration"]["cut_mode"],
                "sequential-only",
            )
            self.assertNotIn("v1-partition", nodes)
            self.assertEqual(
                nodes["v2-partition"]["configuration"][
                    "static_exact_candidate_policy"
                ],
                "assignment-derived-acyclic-v2",
            )
            self.assertEqual(
                nodes["v2-partition"]["configuration"][
                    "minimum_combinational_cut_nets"
                ],
                1,
            )
            for prefix in ("seq", "v2"):
                self.assertEqual(
                    nodes[f"{prefix}-partition"]["configuration"]["seed"],
                    0,
                )
                self.assertEqual(
                    nodes[f"{prefix}-partition"]["configuration"][
                        "seed_attempts"
                    ],
                    1,
                )
            comparison = nodes["static-exact-qor-comparison"]
            self.assertEqual(
                comparison["stage"], "static-exact-qor-compare"
            )
            self.assertIn(
                "src/emuflow/static_exact_qor.py",
                comparison["implementation"]["components"],
            )
            self.assertEqual(comparison["command"].count("--arm"), 2)
            self.assertEqual(
                comparison["configuration"][
                    "generalized_minimum_combinational_cut_nets"
                ],
                1,
            )
            self.assertEqual(comparison["configuration"]["partition_seed"], 0)
            self.assertEqual(
                comparison["configuration"]["partition_seed_attempts"], 1
            )
            self.assertIn(
                "{dependency:v2-phase7-baseline-seed1}",
                comparison["command"],
            )
            self.assertEqual(
                comparison["artifacts"],
                [
                    {
                        "path": "static-exact-qor-comparison.json",
                        "role": "evidence-critical",
                        "retention": "required",
                    }
                ],
            )

    def test_matrix_and_boarddb_materialization_contract_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["clock_periods"] = {"clk": 20.0}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "clock periods"):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "wrong-period.json"
                )

            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["top"] = "renamed_DLA"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "top/clocks"):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "wrong-top.json"
                )

            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            Path(config["platform"]).write_text(
                Path(config["platform"]).read_text() + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "platform bytes"):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "wrong-platform.json"
                )

            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            Path(config["route_constraints"]).write_text(
                Path(config["route_constraints"]).read_text() + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "route constraints bytes"):
                compile_canonical_experiment_spec(
                    config_path, REPOSITORY, root / "wrong-route-constraints.json"
                )


if __name__ == "__main__":
    unittest.main()
