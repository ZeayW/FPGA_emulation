import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.architecture import ArchitectureDB, compatible_cells_for_bel
from emuflow.errors import ImportError, ValidationError
from emuflow.ir import EmuIR
from emuflow.openparf import (
    _lut_size,
    openparf_instance_names,
    resolve_openparf_install,
    run_openparf,
    validate_openparf_runtime,
)
from emuflow.phase2 import run_phase2
from emuflow.placement import Placement, _vivado_regexp_literal
from emuflow.yosys import import_yosys_json


ROOT = Path(__file__).resolve().parents[1]
ARCH_PATH = ROOT / "examples/phase2/xcvu3p_slice_fixture.arch.json"
IR_FIXTURE = ROOT / "examples/yosys/counter.json"


class ArchitectureDBTest(unittest.TestCase):
    def test_openparf_runtime_preflight_rejects_broken_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            installation = root / "openparf-install"
            (installation / "openparf").mkdir(parents=True)
            (installation / "openparf.py").write_text("", encoding="utf-8")
            python = root / "broken-python"
            python.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            python.chmod(0o700)
            with self.assertRaisesRegex(
                Exception, "runtime preflight failed"
            ):
                validate_openparf_runtime(
                    install_root=installation,
                    python_executable=python,
                )

    def test_stable_native_runner_selects_driver_and_requires_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            installation = root / "openparf-install"
            (installation / "openparf").mkdir(parents=True)
            (installation / "openparf.py").write_text("", encoding="utf-8")
            result_dir = root / "results"
            config = root / "openparf.json"
            config.write_text(json.dumps({
                "benchmark_name": "runner",
                "result_dir": str(result_dir),
                "emuflow_stable_global_placement": True,
            }), encoding="utf-8")

            def fake_run(command, **_kwargs):
                self.assertEqual(
                    command[1:3], ["-m", "emuflow.openparf_native_driver"]
                )
                result_dir.mkdir(parents=True)
                (result_dir / "runner.pl").write_text(
                    "i0 0 0 0\n", encoding="utf-8"
                )
                (result_dir / "runner.native-metrics.json").write_text(
                    json.dumps({
                        "schema": "emuflow.openparf-native-convergence/v1",
                        "status": "pass",
                        "stop_reason": "feasible-hpwl-patience",
                        "iterations": 42,
                        "restored_best_feasible": True,
                        "best_feasible_hpwl": 10.0,
                        "final_legal_hpwl": 11.0,
                    }),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=0)

            with mock.patch(
                "emuflow.openparf.subprocess.run", side_effect=fake_run
            ):
                placement = run_openparf(
                    config,
                    install_root=installation,
                    python_executable=Path(sys.executable),
                )
            self.assertEqual(placement, (result_dir / "runner.pl").resolve())

    def test_secondary_ultrascale_ff_bel_is_supported(self) -> None:
        self.assertEqual(
            compatible_cells_for_bel("AFF2", "FF"),
            ["FDCE", "FDPE", "FDRE", "FDSE"],
        )

    def test_lut1_is_supported(self) -> None:
        self.assertEqual(_lut_size("LUT1"), 2)

    def test_fixture_summary(self) -> None:
        architecture = ArchitectureDB.load(ARCH_PATH)
        summary = architecture.summary()
        self.assertEqual(summary["part"], "xcvu3p-ffvc1517-2-e")
        self.assertEqual(summary["sites"], 2)
        self.assertEqual(summary["cell_slots"]["LUT2"], 16)
        self.assertEqual(summary["cell_slots"]["FDRE"], 16)

    def test_import_vivado_tsv(self) -> None:
        content = "\n".join(
            [
                "META\tpart\txcvu3p-ffvc1517-2-e",
                "META\tvivado_version\t2025.2",
                "SITE\tSLICE_X4Y7\tSLICEL\t4\t7",
                "BEL\tA6LUT\tLUT6\t0",
                "BEL\tAFF\tFF\t0",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "arch.tsv"
            path.write_text(content + "\n", encoding="utf-8")
            architecture = ArchitectureDB.from_vivado_tsv(path)
        self.assertEqual(architecture.summary()["sites"], 1)
        self.assertEqual(
            architecture.site_at(4, 7)["name"],  # type: ignore[index]
            "SLICE_X4Y7",
        )


class PlacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.architecture = ArchitectureDB.load(ARCH_PATH)
        self.ir = import_yosys_json(IR_FIXTURE, top="counter", clocks=["clk"])

    def test_openparf_round_trip_and_xdc(self) -> None:
        reference = Placement.greedy_reference(self.architecture, self.ir)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "result.pl"
            path.write_text(reference.to_openparf_pl(), encoding="utf-8")
            imported = Placement.from_openparf_pl(
                path, self.architecture, self.ir
            )
        self.assertEqual(imported.summary()["status"], "legal")
        self.assertEqual(imported.summary()["cells"], 8)
        self.assertIn("set_property LOC", imported.to_xdc())
        self.assertIn("# EmuIR instance: next_lut[0]", imported.to_xdc())
        self.assertIn(r"\x6e\x65\x78\x74\x5f\x6c\x75\x74", imported.to_xdc())
        self.assertNotIn("\nif {", imported.to_xdc())
        self.assertEqual(imported.to_xdc().count("set_property BEL"), 4)
        tsv_lines = imported.to_vivado_tsv().splitlines()
        self.assertEqual(len(tsv_lines), 9)
        fields = tsv_lines[1].split("\t")
        self.assertEqual(int(fields[0]), 0)
        self.assertTrue(bytes.fromhex(fields[1]).decode("utf-8"))
        self.assertTrue(fields[2].startswith("SLICE_"))

    def test_vivado_mapped_name_doubles_yosys_backslashes(self) -> None:
        encoded = _vivado_regexp_literal("$flatten\\cpu")
        self.assertIn(r"\x5c\x5c", encoded)

    def test_bookshelf_safe_names_are_restored(self) -> None:
        reference = Placement.greedy_reference(self.architecture, self.ir)
        name_map = openparf_instance_names(self.ir)
        lines = []
        for cell in reference.value["cells"]:
            lines.append(
                f"{name_map[cell['instance']]} {cell['x']} {cell['y']} "
                f"{cell['z']}"
            )
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "encoded.pl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            imported = Placement.from_openparf_pl(
                path, self.architecture, self.ir
            )
        self.assertEqual(imported.summary()["cells"], 8)

    def test_illegal_openparf_coordinate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bad.pl"
            path.write_text("next_lut[0] 99 99 0\n", encoding="utf-8")
            with self.assertRaisesRegex(ImportError, "no architecture site"):
                Placement.from_openparf_pl(path, self.architecture, self.ir)

    def test_bel_collision_is_rejected(self) -> None:
        reference = Placement.greedy_reference(
            self.architecture, self.ir
        ).to_dict()
        reference["cells"][1]["site"] = reference["cells"][0]["site"]
        reference["cells"][1]["bel"] = reference["cells"][0]["bel"]
        reference["cells"][1]["x"] = reference["cells"][0]["x"]
        reference["cells"][1]["y"] = reference["cells"][0]["y"]
        reference["cells"][1]["z"] = reference["cells"][0]["z"]
        reference["cells"][1]["cell_type"] = reference["cells"][0]["cell_type"]
        with self.assertRaises(ValidationError):
            Placement(reference, self.architecture)

    def test_openparf_global_coordinates_are_archdb_legalized(self) -> None:
        names = openparf_instance_names(self.ir)
        lines = [
            f"{names[instance['id']]} 0.25 0.75 0"
            for instance in self.ir.value["instances"]
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "global.pl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            placement = Placement.from_openparf_global_pl(
                path,
                self.architecture,
                self.ir,
                tile_size=2,
                site_utilization_limit=1.0,
                site_y_range=(0, 0),
            )
        self.assertEqual(placement.summary()["status"], "legal")
        self.assertEqual(placement.summary()["cells"], 8)
        self.assertIn(
            "openparf-global-bookshelf-pl",
            placement.value["source"]["format"],
        )
        self.assertEqual(
            placement.value["source"]["site_utilization_limit"], 1.0
        )
        self.assertEqual(placement.value["source"]["site_y_range"], [0, 0])
        self.assertEqual(
            placement.value["source"]["y_transform"],
            "affine-to-site-y-range",
        )
        self.assertGreaterEqual(
            placement.value["source"]["max_manhattan_displacement"], 0
        )


class Phase2PipelineTest(unittest.TestCase):
    def test_openparf_resolver_accepts_monorepo_install_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prefix = Path(temporary_directory) / "install"
            package = prefix / "openparf"
            (package / "openparf").mkdir(parents=True)
            (package / "openparf.py").write_text("", encoding="utf-8")
            self.assertEqual(
                resolve_openparf_install(prefix), package.resolve()
            )

    def test_root_built_openparf_runner_returns_expected_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            installation = root / "install"
            (installation / "openparf").mkdir(parents=True)
            driver = installation / "openparf.py"
            driver.write_text(
                "\n".join(
                    (
                        "import argparse",
                        "import json",
                        "from pathlib import Path",
                        "parser = argparse.ArgumentParser()",
                        "parser.add_argument('--config', required=True)",
                        "parser.add_argument('--log', required=True)",
                        "args = parser.parse_args()",
                        "config = json.loads(",
                        "    Path(args.config).read_text(encoding='utf-8')",
                        ")",
                        "result = Path(config['result_dir'])",
                        "result.mkdir(parents=True, exist_ok=True)",
                        "(result / (config['benchmark_name'] + '.pl')).write_text(",
                        "    'i0 0 0 0\\n', encoding='utf-8'",
                        ")",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            config = root / "openparf.json"
            result_dir = root / "results"
            config.write_text(
                json.dumps(
                    {
                        "benchmark_name": "runner",
                        "result_dir": str(result_dir),
                    }
                ),
                encoding="utf-8",
            )
            placement = run_openparf(
                config,
                install_root=installation,
                python_executable=Path(sys.executable),
            )
            self.assertEqual(
                placement, (result_dir / "runner.pl").resolve()
            )
            self.assertEqual(
                placement.read_text(encoding="utf-8"),
                "i0 0 0 0\n",
            )

    def test_pipeline_writes_adapter_and_placement_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir = EmuIR(
                import_yosys_json(
                    IR_FIXTURE, top="counter", clocks=["clk"]
                ).to_dict()
            )
            ir_path = root / "counter.emuir.json"
            ir_path.write_text(
                json.dumps(ir.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
            output = root / "phase2"
            report = run_phase2(
                ir_path,
                ARCH_PATH,
                output,
                reference_placement=True,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["provider"], "emuflow-greedy-reference")
            for filename in (
                "phase2_report.json",
                "placement.json",
                "placement.xdc",
                "placement.vivado.tsv",
                "normalized.pl",
                "openparf/design.aux",
                "openparf/design.lib",
                "openparf/design.nets",
                "openparf/design.nodes",
                "openparf/design.scl",
                "openparf/openparf.json",
                "openparf/manifest.json",
                "openparf/name_map.json",
            ):
                self.assertTrue((output / filename).is_file(), filename)
            scl = (output / "openparf/design.scl").read_text(encoding="utf-8")
            self.assertIn("LUT LUT2", scl)
            self.assertIn("FF FDRE", scl)
            self.assertNotIn("FDCE", scl)
            library = (output / "openparf/design.lib").read_text(
                encoding="utf-8"
            )
            self.assertIn("PIN CE INPUT CTRL_CE", library)
            self.assertIn("PIN R INPUT CTRL_SR", library)
            config = json.loads(
                (output / "openparf/openparf.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                config["gp_model2area_types_map"]["LUT2"]["isLUT"], 2
            )
            self.assertEqual(config["architecture_name"], "ultrascale")
            self.assertEqual(config["dtype"], "float64")
            self.assertEqual(config["target_density"], 0.8)
            self.assertEqual(config["detailed_place_flag"], 0)
            self.assertEqual(config["plot_target_at_names"], ["FF", "LUT"])
            nodes = (output / "openparf/design.nodes").read_text(
                encoding="utf-8"
            )
            self.assertTrue(nodes.startswith("i0 "))
            name_map = json.loads(
                (output / "openparf/name_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(name_map["schema"], "emuflow.openparf-name-map/v1")


if __name__ == "__main__":
    unittest.main()
