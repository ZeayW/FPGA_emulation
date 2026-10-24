import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from emuflow.xilinx_lut_depopulation import spread_hot_slice_sites
from tests import test_xilinx_lut_depopulation as depopulation_tests


class NativeSliceSpreadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("c++")
        if compiler is None:
            raise unittest.SkipTest("C++17 compiler unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.build.name)/"spread"
        subprocess.run([compiler, "-std=c++17", "-O2",
                        str(Path(__file__).parents[1]/"src/native/xilinx_slice_spreading.cpp"),
                        "-o", str(cls.exe)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_case(self, root, *, protected=None, fallback=False, corrupt=None):
        sites, atoms, placement, output = [root/n for n in ("sites", "atoms", "in.pl", "out.pl")]
        sites.write_text("".join(f"{x+100} 10 {x} 0 1\n" for x in range(9)))
        ar, pr = [], []
        for x in (5, 6):
            for i, (resource, primitive, slot) in enumerate((("LUT", "LUT6", 1), ("LUT", "LUT3", 3), ("FF", "FDCE", 0))):
                name = f"a{x}_{i}"
                cluster = "macro" if x == 5 and protected == "macro" and i < 2 else name
                ar.append(f"{name} {cluster} {resource} {primitive}\n")
                flag = " /FIXED" if x == 5 and protected == "fixed" and i == 0 else ""
                pr.append(f"{name} {x+100} 10 {slot}{flag}\n")
        if fallback:
            ar.append("fixed fixed LUT LUT6\n"); pr.append("fixed 108 10 1 /FIXED\n")
        if corrupt == "lut_collision":
            pr[1] = "a5_1 105 10 1\n"
        elif corrupt == "missing":
            pr.pop()
        elif corrupt == "ff_collision":
            ar.append("bad bad FF FDRE\n");pr.append("bad 105 10 0\n")
        atoms.write_text("".join(ar));placement.write_text("".join(pr));before=placement.read_bytes()
        result = subprocess.run([str(self.exe), str(sites), str(atoms), str(placement), str(output), "2", "1", "0"], capture_output=True, text=True)
        self.assertEqual(placement.read_bytes(), before)
        return result, output, placement

    def test_moves_whole_slices_and_preserves_bel_slots(self):
        with tempfile.TemporaryDirectory() as d:
            result, output, _ = self.run_case(Path(d))
            self.assertEqual(result.returncode, 0, result.stderr)
            counters = json.loads(result.stdout)
            self.assertEqual((counters["moved_slices"], counters["moved_luts"], counters["moved_ffs"]), (2, 4, 2))
            rows = {v[0]: v[1:] for v in map(str.split, output.read_text().splitlines())}
            for x, target in ((5, 106), (6, 108)):
                for i, slot in enumerate((1, 3, 0)):
                    self.assertEqual(rows[f"a{x}_{i}"], [str(target), "10", str(slot)])
            self.assertEqual(len(rows), 6)

    def test_fallback_evicts_to_original_sites_without_collisions(self):
        with tempfile.TemporaryDirectory() as d:
            result, output, source = self.run_case(Path(d), fallback=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            counters = json.loads(result.stdout)
            self.assertEqual(counters["fallback_slices"], 1)
            self.assertEqual(counters["fallback_reassignments"], 1)
            self.assertEqual(counters["moved_slices"], 0)
            self.assertEqual(output.read_bytes(), source.read_bytes())

    def test_fixed_and_macro_bearing_slices_are_preserved(self):
        for protected in ("fixed", "macro"):
            with self.subTest(protected=protected), tempfile.TemporaryDirectory() as d:
                result, output, _ = self.run_case(Path(d), protected=protected)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = {v[0]: v[1:] for v in map(str.split, output.read_text().splitlines())}
                self.assertTrue(all(rows[f"a5_{i}"][:2] == ["105", "10"] for i in range(3)))
                self.assertEqual(json.loads(result.stdout)["moved_slices"], 1)

    def test_invalid_atom_coverage_or_bel_collision_is_rejected(self):
        for corrupt in ("lut_collision", "ff_collision", "missing"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as d:
                result, output, _ = self.run_case(Path(d), corrupt=corrupt)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_wrapper_consumes_real_normalized_site_and_atom_contracts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            placement, name_map, kwargs = depopulation_tests.NativeLutDepopulationTest.wrapper_fixture(self, root)
            with patch("emuflow.xilinx_lut_depopulation.resolve_native_executable", return_value=str(self.exe)):
                report = spread_hot_slice_sites(placement, name_map, root/"candidate.pl", **kwargs, hot_clock_regions=["X0Y0"])
            self.assertEqual(report["counters"]["atoms"], 2)
            self.assertEqual(report["schema"], "emuflow.xilinx-slice-spreading/v1")
            self.assertTrue(report["requires_exact_placement_validation"])


if __name__ == "__main__":
    unittest.main()
