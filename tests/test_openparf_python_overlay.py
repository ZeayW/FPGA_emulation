import hashlib
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
            (placement / "placer.py").write_text("baseline\n")
            git("add", "."); git("commit", "-m", "baseline")
            baseline = git("rev-parse", "HEAD")
            (placement / "placer.py").write_text("patched\n")
            (placement / "sll_weight.py").write_text("helper\n")
            git("add", "."); git("commit", "-m", "python fix")
            commit = git("rev-parse", "HEAD")
            (base / "openparf/placement").mkdir(parents=True)
            (base / "openparf/ops").mkdir()
            original = base / "openparf/placement/placer.py"
            original.write_text("baseline\n")
            (base / "openparf/ops/native.so").write_bytes(b"native ABI")
            (base / "openparf.py").write_text("entry\n")
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            result = MODULE.deploy(source, base, target, commit, baseline)
            self.assertEqual(result["source_commit"], commit)
            self.assertEqual((target / "openparf/placement/placer.py").resolve(), (placement / "placer.py").resolve())
            self.assertEqual((target / "openparf/placement/sll_weight.py").resolve(), (placement / "sll_weight.py").resolve())
            self.assertEqual((target / "openparf/ops/native.so").read_bytes(), b"native ABI")
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), digest)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                MODULE.deploy(source, base, target, commit, baseline)
            original.write_text("different base\n")
            with self.assertRaisesRegex(RuntimeError, "different placement"):
                MODULE.deploy(source, base, root / "other", commit, baseline)
            original.write_text("baseline\n")
            (source / "engines/openparf/native.cpp").write_text("ABI change\n")
            git("add", "."); git("commit", "-m", "native change")
            with self.assertRaisesRegex(RuntimeError, "Python-only"):
                MODULE.deploy(source, base, root / "other", git("rev-parse", "HEAD"), baseline)
