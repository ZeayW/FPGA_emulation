import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from emuflow.canonical_experiment import (
    CANONICAL_EXPERIMENT_CONFIG_SCHEMA,
    compile_canonical_experiment_spec,
)
from emuflow.experiment_dag import validate_experiment_spec
from emuflow.errors import ValidationError
from emuflow.experiment_identity import build_implementation_closure
from emuflow.io import write_json
from emuflow.tdm import TDM_STATIC_EXACT_PROVIDER
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
            report = compile_canonical_experiment_spec(
                self._config(root), REPOSITORY, output
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
                self.assertEqual(roles["physical"], "diagnostic")
            self.assertTrue(
                all(
                    item["validator"][-6:]
                    == [
                        "--seed",
                        str(item["physical_seed"]),
                        "--workers",
                        "8",
                        "--route-channel-width",
                        "300",
                    ]
                    for item in terminals
                )
            )

    def test_physical_storage_peak_override_is_sealed_and_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
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
                    "max_cross_fpga_dependency_depth": 2,
                    "comb_segment_budget_slots": 2,
                    "route_candidate_workers": 7,
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "spec.json"
            report = compile_canonical_experiment_spec(
                config_path, REPOSITORY, output
            )
            self.assertEqual(report["physical_terminal_nodes"], 1)
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
            self.assertNotIn("phase6-placement-aware", nodes)
            self.assertNotIn("phase6-chimew", nodes)
            self.assertNotIn("qor-comparison", nodes)
            partition = nodes["partition"]
            self.assertEqual(
                partition["configuration"]["cut_mode"],
                "static-exact-combinational",
            )
            self.assertEqual(
                partition["configuration"]["comb_segment_budget_slots"], 2
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
            self.assertEqual(
                route["configuration"]["provider"],
                NATIVE_TIMING_EVALUATED_PROVIDER,
            )
            self.assertEqual(route["configuration"]["candidate_workers"], 1)
            tdm = nodes["tdm"]
            self.assertEqual(
                tdm["configuration"]["provider"],
                TDM_STATIC_EXACT_PROVIDER,
            )
            self.assertNotIn("--ratio-optimizer", tdm["command"])
            self.assertNotIn(
                "ratio_plan.json",
                {item["path"] for item in tdm["artifacts"]},
            )
            terminals = [
                node for node in nodes.values() if node["stage"] == "phase7"
            ]
            self.assertEqual(
                {(item["provider"], item["physical_seed"]) for item in terminals},
                {("baseline", 1)},
            )

    def test_multiple_physical_seeds_remain_an_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
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
