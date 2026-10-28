import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.platform.architecture import ArchitectureDB, compatible_cells_for_bel
from emuflow.physical.openparf.openparf import (
    _openparf_environment,
    resolve_openparf_install,
    validate_openparf_runtime,
)


ROOT = Path(__file__).resolve().parents[1]
ARCH_PATH = ROOT / "examples/phase2/xcvu3p_slice_fixture.arch.json"


class OpenparfRuntimeAndArchitectureTest(unittest.TestCase):
    def test_openparf_environment_selects_the_child_torch_abi(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            installation = root / "openparf-install"
            installation.mkdir()
            python = root / "child" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            child_torch = (
                root / "child" / "lib" / "python3.8"
                / "site-packages" / "torch" / "lib"
            )
            child_torch.mkdir(parents=True)
            foreign_torch = (
                root / "coordinator" / "lib" / "python3.10"
                / "site-packages" / "torch" / "lib"
            )
            foreign_torch.mkdir(parents=True)
            preserved = root / "native-libraries"
            with mock.patch.dict(
                "os.environ",
                {"LD_LIBRARY_PATH": os.pathsep.join((
                    str(foreign_torch), str(preserved),
                ))},
                clear=False,
            ):
                environment = _openparf_environment(
                    installation, python
                )

        paths = environment["LD_LIBRARY_PATH"].split(os.pathsep)
        self.assertEqual(paths[:2], [
            str(root / "child" / "lib"), str(child_torch),
        ])
        self.assertNotIn(str(foreign_torch), paths)
        self.assertIn(str(preserved), paths)

    def test_openparf_environment_prefers_explicit_cpp_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            installation = root / "openparf-install"
            installation.mkdir()
            python = root / "child" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            runtime = root / "gcc-runtime"
            runtime.mkdir()
            with mock.patch.dict(
                "os.environ",
                {"EMUFLOW_OPENPARF_LIBRARY_PATH": str(runtime)},
                clear=False,
            ):
                environment = _openparf_environment(
                    installation, python
                )

        paths = environment["LD_LIBRARY_PATH"].split(os.pathsep)
        self.assertEqual(paths[0], str(runtime))
        self.assertEqual(paths[1], str(root / "child" / "lib"))

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



    def test_secondary_ultrascale_ff_bel_is_supported(self) -> None:
        self.assertEqual(
            compatible_cells_for_bel("AFF2", "FF"),
            ["FDCE", "FDPE", "FDRE", "FDSE"],
        )


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




class OpenparfRunnerTest(unittest.TestCase):
    def test_openparf_resolver_accepts_monorepo_install_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prefix = Path(temporary_directory) / "install"
            package = prefix / "openparf"
            (package / "openparf").mkdir(parents=True)
            (package / "openparf.py").write_text("", encoding="utf-8")
            self.assertEqual(
                resolve_openparf_install(prefix), package.resolve()
            )




if __name__ == "__main__":
    unittest.main()
