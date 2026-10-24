import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ENTRY = Path(__file__).parents[1] / "src/emuflow/openparf_driver_entry.py"


class OpenparfDriverEntryTest(unittest.TestCase):
    def test_drivers_load_without_emuflow_in_the_child_search_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            for driver in ("openparf_native_driver", "openparf_continuous_driver"):
                with self.subTest(driver=driver):
                    result = subprocess.run(
                        [sys.executable, "-I", str(ENTRY), driver, "--help"],
                        cwd=temporary, text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--config", result.stdout)

    def test_calling_driver_keeps_the_child_dependency_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "coordinator-site-packages/emuflow"
            package.mkdir(parents=True)
            shutil.copyfile(ENTRY, package / ENTRY.name)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "openparf.py").write_text(
                "raise RuntimeError('adapter shadowed runtime OpenPARF')\n",
                encoding="utf-8",
            )
            (package.parent / "torch.py").write_text(
                "raise RuntimeError('coordinator Torch shadowed child ABI')\n",
                encoding="utf-8",
            )
            (package / "openparf_native_driver.py").write_text(
                "import openparf, torch, sys\n"
                "assert openparf.ORIGIN == torch.ORIGIN == 'child-runtime'\n"
                "assert sys.argv[1:] == ['--sentinel', 'value']\n"
                "print('calling-driver-with-child-dependencies')\n",
                encoding="utf-8",
            )
            runtime = root / "child-runtime"
            runtime.mkdir()
            for name in ("openparf", "torch"):
                (runtime / (name + ".py")).write_text(
                    "ORIGIN='child-runtime'\n", encoding="utf-8"
                )
            (runtime / "emuflow").mkdir()
            (runtime / "emuflow/__init__.py").write_text(
                "raise RuntimeError('older child EmuFlow was selected')\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(package / ENTRY.name),
                 "openparf_native_driver", "--sentinel", "value"],
                cwd=root, env={**os.environ, "PYTHONPATH": str(runtime)},
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "calling-driver-with-child-dependencies")
