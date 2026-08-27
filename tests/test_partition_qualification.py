import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.cli import main
from emuflow.contest_validation_matrix import load_contest_validation_matrix
from emuflow.errors import ValidationError
from emuflow.end_to_end_validation_matrix import (
    load_end_to_end_validation_matrix,
)
from emuflow.partition_qualification import (
    PARTITION_QUALIFICATION_CONFIG_SCHEMA,
    compile_partition_qualification_spec,
)


REPOSITORY = Path(__file__).resolve().parents[1]


class PartitionQualificationTest(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        rtl = root / "dla_like.medium.v"
        rtl.write_text("module DLA(input clk); endmodule\n", encoding="utf-8")

        platform = root / "boarddb.json"
        platform_value = json.loads(
            (
                REPOSITORY / "platforms/virtual/xcvu3p_2fpga_p2p.json"
            ).read_text()
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
                            "sha256": hashlib.sha256(
                                platform.read_bytes()
                            ).hexdigest(),
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

        tool_names = (
            "emuflow",
            "hop_refiner",
            "mfspart_coarsener",
            "mfspart_initializer",
            "mfspart_legalizer",
            "mfspart_refiner",
            "mfspart_refiner_checker",
            "opensta",
            "yosys",
        )
        tools = {}
        for name in tool_names:
            path = root / f"tool-{name}"
            path.write_text(f"fixture {name}\n", encoding="utf-8")
            tools[name] = str(path)

        config = {
            "schema": PARTITION_QUALIFICATION_CONFIG_SCHEMA,
            "case_id": "koios-dla-medium-l5__eda2023-case6",
            "source_commit": "a" * 40,
            "rtl_source": str(rtl),
            "platform": str(platform),
            "boarddb_report": str(boarddb_report),
            "route_constraints": str(route_constraints),
            "timing_model": str(
                REPOSITORY
                / "resources/timing/ultrascaleplus-softlogic-v1.json"
            ),
            "architecture_timing_db": str(
                REPOSITORY
                / "resources/architectures/vtr/flagship-k6-n10-40nm.json"
            ),
            "tools": tools,
            "top": "DLA",
            "clocks": ["clk"],
            "clock_periods": {"clk": 10.0},
            "partition_provider": "mfspart",
            "partition_seed": 0,
            "partition_seed_attempts": 1,
            "partition_repair_balance": False,
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_compiler_emits_sealed_three_node_mfspart_dag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "spec.json"
            report = compile_partition_qualification_spec(
                self._config(root), REPOSITORY, output
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["nodes"], 3)
            self.assertEqual(report["terminal_node"], "partition")

            spec = json.loads(output.read_text())
            self.assertEqual(
                [node["id"] for node in spec["nodes"]],
                ["frontend", "timing", "partition"],
            )
            partition = spec["nodes"][2]
            _, matrix_validation = load_end_to_end_validation_matrix(
                REPOSITORY / "benchmarks/end_to_end_validation_matrix.json"
            )
            self.assertEqual(
                spec["nodes"][0]["inputs"]["end_to_end_matrix"],
                matrix_validation["matrix_sha256"],
            )
            self.assertEqual(partition["dependencies"], ["frontend", "timing"])
            self.assertEqual(partition["configuration"]["provider"], "mfspart")
            self.assertFalse(partition["configuration"]["repair_balance"])
            self.assertNotIn("--openroad", partition["command"])
            for option, tool in (
                ("--mfspart-coarsener", "tool.mfspart_coarsener"),
                ("--mfspart-initializer", "tool.mfspart_initializer"),
                ("--mfspart-refiner", "tool.mfspart_refiner"),
                ("--mfspart-refiner-checker", "tool.mfspart_refiner_checker"),
                ("--mfspart-legalizer", "tool.mfspart_legalizer"),
            ):
                index = partition["command"].index(option) + 1
                self.assertEqual(
                    partition["command_identity"][index], f"{{input:{tool}}}"
                )
                self.assertIn(tool, partition["inputs"])
            self.assertIn(
                "src/emuflow/mfspart_initial.py",
                partition["implementation"]["components"],
            )
            self.assertIn(
                {"path": "mfspart", "role": "consumer-checkpoint", "retention": "required"},
                partition["artifacts"],
            )

    def test_mfspart_qualification_policy_is_not_silently_weakened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._config(root)
            config = json.loads(config_path.read_text())
            config["partition_provider"] = "tritonpart"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "partition_provider"):
                compile_partition_qualification_spec(
                    config_path, REPOSITORY, root / "provider.json"
                )

            config["partition_provider"] = "mfspart"
            config["partition_seed_attempts"] = 2
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "exactly one"):
                compile_partition_qualification_spec(
                    config_path, REPOSITORY, root / "attempts.json"
                )

            config["partition_seed_attempts"] = 1
            config["partition_repair_balance"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "forbids"):
                compile_partition_qualification_spec(
                    config_path, REPOSITORY, root / "repair.json"
                )

    @mock.patch("emuflow.cli.run_partition_checkpoint")
    def test_partition_stage_forwards_explicit_mfspart_tools(self, run) -> None:
        run.return_value = {"status": "pass"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments = [
                "experiment-stage",
                "partition-run",
                "--frontend",
                str(root / "frontend"),
                "--timing",
                str(root / "timing"),
                "--platform",
                str(root / "platform.json"),
                "--out",
                str(root / "output"),
            ]
            expected = {}
            for name in (
                "coarsener",
                "initializer",
                "refiner",
                "refiner-checker",
                "legalizer",
            ):
                value = str(root / name)
                arguments.extend((f"--mfspart-{name}", value))
                expected[name.replace("-", "_")] = value
            self.assertEqual(main(arguments), 0)
        kwargs = run.call_args.kwargs
        for name, value in expected.items():
            self.assertEqual(kwargs[f"mfspart_{name}"], value)


if __name__ == "__main__":
    unittest.main()
