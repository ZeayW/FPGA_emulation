import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpenParfSparseLegalizerPatchTests(unittest.TestCase):
    def test_patch_applies_to_pinned_openparf_and_prefers_nearest_site(self):
        patch = shutil.which("patch")
        if patch is None:
            self.skipTest("patch executable is unavailable")
        source = (
            ROOT
            / "engines/openparf/openparf/ops/direct_lg/src/dl_solver.cpp"
        )
        patch_file = ROOT / "patches/openparf/sparse-greedy-first.patch"
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            target = (
                checkout
                / "openparf/ops/direct_lg/src/dl_solver.cpp"
            )
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
            with patch_file.open("rb") as patch_input:
                subprocess.run(
                    [patch, "--batch", "--directory", str(checkout), "-p1"],
                    stdin=patch_input,
                    check=True,
                )
            text = target.read_text(encoding="utf-8")
        greedy_body = text.split(
            "bool DLSolver::greedyLegalizeInst(DLInstance &inst) {", 1
        )[1].split("/// Print the statistics for the DL solution", 1)[0]
        self.assertIn(
            "if (!greedyLegalizeInst(inst) && "
            "!ripupLegalizeInst(inst, _param.nbrDistEnd))",
            text,
        )
        self.assertIn("auto site_id = siteIdAtSearchCoordinate", greedy_body)
        self.assertIn("_siteMap[site_id]", greedy_body)
        self.assertIn("site.det   = cand;", greedy_body)
        self.assertNotIn("RealType     bestScoreImprov", greedy_body)


if __name__ == "__main__":
    unittest.main()
