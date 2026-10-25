import hashlib
import json
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "openparf_python_overlay", ROOT / "scripts/validation/deploy_openparf_python_overlay.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OpenParfPythonOverlayTests(unittest.TestCase):
    def test_exact_source_links_preserve_native_base_and_reject_abi_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, base, target = root / "source", root / "native", root / "output"
            source.mkdir()
            def git(*args):
                return subprocess.check_output(
                    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                    cwd=source, stderr=subprocess.PIPE,
                ).decode().strip()
            git("init")
            placement = source / "engines/openparf/openparf/placement"
            placement.mkdir(parents=True)
            params = source / "engines/openparf/openparf/params.json"
            ism = source / "engines/openparf/openparf/ops/ism_dp"
            ism.mkdir(parents=True)
            (ism / "ism_dp.py").write_text("baseline ism\n")
            params.write_text("{\"step\": 1.002}\n")
            (placement / "placer.py").write_text("baseline\n")
            (placement / "data_collections.py").write_text("baseline data\n")
            (placement / "filler.py").write_text("baseline filler\n")
            git("add", "."); git("commit", "-m", "baseline")
            baseline = git("rev-parse", "HEAD")
            (placement / "placer.py").write_text("patched\n")
            (placement / "sll_weight.py").write_text("helper\n")
            (placement / "data_collections.py").write_text("bounded data\n")
            (placement / "filler.py").write_text("bounded filler\n")
            params.write_text("{\"step\": 0.002}\n")
            git("add", "."); git("commit", "-m", "python fix")
            commit = git("rev-parse", "HEAD")
            (base / "openparf/placement").mkdir(parents=True)
            (base / "openparf/ops").mkdir()
            native_ism_dir = base / "openparf/ops/ism_dp"
            native_ism_dir.mkdir()
            (native_ism_dir / "ism_dp.py").write_text("baseline ism\n")
            module_name = "ism_dp_cpp.cpython-38-x86_64-linux-gnu.so"
            (native_ism_dir / module_name).write_bytes(b"old ISM")
            (base / "openparf/params.json").write_text("{\"step\": 1.002}\n")
            original = base / "openparf/placement/placer.py"
            original.write_text("baseline\n")
            (base / "openparf/placement/data_collections.py").write_text(
                "baseline data\n"
            )
            (base / "openparf/placement/filler.py").write_text(
                "baseline filler\n"
            )
            (base / "openparf/ops/native.so").write_bytes(b"native ABI")
            (base / "openparf.py").write_text("entry\n")
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            result = MODULE.deploy(source, base, target, commit, baseline)
            self.assertEqual(result["source_commit"], commit)
            self.assertEqual((target / "openparf/placement/placer.py").resolve(), (placement / "placer.py").resolve())
            self.assertEqual((target / "openparf/placement/sll_weight.py").resolve(), (placement / "sll_weight.py").resolve())
            self.assertEqual((target / "openparf/placement/data_collections.py").resolve(), (placement / "data_collections.py").resolve())
            self.assertEqual((target / "openparf/placement/filler.py").resolve(), (placement / "filler.py").resolve())
            self.assertEqual((target / "openparf/params.json").resolve(), params.resolve())
            self.assertEqual((target / "openparf/ops/native.so").read_bytes(), b"native ABI")
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), digest)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                MODULE.deploy(source, base, target, commit, baseline)
            original.write_text("different base\n")
            with self.assertRaisesRegex(RuntimeError, "different placement"):
                MODULE.deploy(source, base, root / "other", commit, baseline)
            original.write_text("baseline\n")
            header = ism / "src/ism_detailed_placer.hpp"
            header.parent.mkdir()
            header.write_text("fresh utilization\n")
            git("add", "."); git("commit", "-m", "native ISM fix")
            ism_commit = git("rev-parse", "HEAD")
            with self.assertRaisesRegex(RuntimeError, "Python-only"):
                MODULE.deploy(source, base, root / "ism-output", ism_commit, baseline)
            rebuilt = root / module_name
            rebuilt.write_bytes(b"rebuilt ISM")
            receipt = dict(status="built", provider="ism-utilization-capacity-and-fresh-demand-v1",
                           source_commit=ism_commit, source_root=str(source.resolve()),
                           binary=dict(path=str(rebuilt), sha256=hashlib.sha256(rebuilt.read_bytes()).hexdigest()))
            receipt_path = root / "build.json"
            receipt_path.write_text(json.dumps(receipt))
            MODULE.deploy(source, base, root / "ism-output", ism_commit, baseline, receipt_path)
            installed = root / "ism-output/openparf/ops/ism_dp"
            self.assertEqual((installed / module_name).resolve(), rebuilt.resolve())
            self.assertEqual((installed / "ism_dp.py").resolve(), (ism / "ism_dp.py").resolve())
            rebuilt.write_bytes(b"tampered ISM")
            with self.assertRaisesRegex(RuntimeError, "does not match its build receipt"):
                MODULE.deploy(source, base, root / "tampered", ism_commit, baseline, receipt_path)
            (source / "engines/openparf/native.cpp").write_text("ABI change\n")
            git("add", "."); git("commit", "-m", "native change")
            with self.assertRaisesRegex(RuntimeError, "Python-only"):
                MODULE.deploy(source, base, root / "other", git("rev-parse", "HEAD"), baseline)
