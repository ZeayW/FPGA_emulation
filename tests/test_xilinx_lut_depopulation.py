import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.xilinx_lut_depopulation import _hot_region_boxes, depopulate_hot_lut_sites


class NativeLutDepopulationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("c++")
        if compiler is None:
            raise unittest.SkipTest("C++17 compiler unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.build.name) / "depopulate"
        subprocess.run([compiler, "-std=c++17", "-O2",
                        str(Path(__file__).parents[1]/"src/native/xilinx_lut_depopulation.cpp"),
                        "-o", str(cls.exe)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def run_case(self, root, *, macro=False, fixed=False, radius=4, corrupt=None):
        sites = root/"sites.tsv"
        atoms = root/"atoms.tsv"
        placement = root/"input.pl"
        output = root/"output.pl"
        sites.write_text("0 0 1\n1 0 0\n2 0 0\n")
        atom_rows, pl_rows = [], []
        for i in range(8):
            cluster = "macro" if macro and i < 2 else f"singleton{i}"
            atom_rows.append(f"a{i} {cluster} LUT LUT6\n")
            flag = " /FIXED" if fixed and i == 0 else ""
            pl_rows.append(f"a{i} 0 0 {2*i+1}{flag}\n")
        atom_rows.append("ff ff FF FDCE\n")
        pl_rows.append("ff 0 0 0\n")
        if corrupt == "collision":
            pl_rows[1] = "a1 0 0 1\n"
        if corrupt == "missing":
            pl_rows.pop()
        atoms.write_text("".join(atom_rows))
        placement.write_text("".join(pl_rows))
        before = placement.read_bytes()
        completed = subprocess.run(
            [str(self.exe), str(sites), str(atoms), str(placement), str(output), "6", str(radius)],
            capture_output=True, text=True,
        )
        self.assertEqual(placement.read_bytes(), before)
        return completed, output

    def test_moves_only_excess_singleton_luts_and_preserves_every_atom(self):
        with tempfile.TemporaryDirectory() as d:
            completed, output = self.run_case(Path(d))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["moved_luts"], 2)
            self.assertEqual(report["unrelieved_movable_luts"], 0)
            self.assertEqual(report["maximum_manhattan_distance"], 1)
            rows = {f[0]: f[1:] for f in map(str.split, output.read_text().splitlines())}
            self.assertEqual(set(rows), {"ff", *(f"a{i}" for i in range(8))})
            self.assertEqual(rows["ff"], ["0", "0", "0"])
            self.assertEqual(sum(v[:2] == ["0", "0"] for k,v in rows.items() if k != "ff"), 6)
            slots = {(v[0], v[1], v[2]) for k,v in rows.items() if k != "ff"}
            self.assertEqual(len(slots), 8)

    def test_macro_and_fixed_sites_are_preserved(self):
        for options in ({"macro": True}, {"fixed": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as d:
                completed, output = self.run_case(Path(d), **options)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = json.loads(completed.stdout)
                self.assertEqual(report["moved_luts"], 0)
                self.assertEqual(report["protected_hot_sites"], 1)
                self.assertEqual(output.read_bytes(), (Path(d)/"input.pl").read_bytes())

    def test_exhausted_bound_reports_unrelieved_without_losing_cells(self):
        with tempfile.TemporaryDirectory() as d:
            completed, output = self.run_case(Path(d), radius=0)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["unrelieved_movable_luts"], 2)
            self.assertEqual(output.read_bytes(), (Path(d)/"input.pl").read_bytes())

    def test_invalid_placement_never_publishes_a_candidate(self):
        for corrupt in ("collision", "missing"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as d:
                completed, output = self.run_case(Path(d), corrupt=corrupt)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(output.exists())

    def test_wrapper_reads_regions_from_contract_without_per_site_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.run_case(root)
            atoms = [dict(zip(("openparf", "source_cluster", "resource", "cell_type"),
                              line.split())) for line in (root/"atoms.tsv").read_text().splitlines()]
            name_map = {"atoms": atoms, "coordinate_system": {"clock_regions": {"regions": [
                {"name": "X0Y0", "source_name": "X6Y9", "bbox": [0, 0, 0, 0]},
                {"name": "X1Y0", "source_name": "X7Y9", "bbox": [1, 0, 2, 0]},
            ]}}}
            sites = [dict(dense_x=x, dense_y=0, resources={"LUT": 8},
                          physical_sites={"LUT": [f"SLICE_X{x}Y0"]}) for x in range(3)]
            module = "emuflow.xilinx_lut_depopulation"
            with patch(module+".resolve_native_executable", return_value=str(self.exe)), \
                 patch(module+".read_xilinx_openparf_atomic_name_map", return_value=name_map), \
                 patch(module+".load_xilinx_openparf_atomic_sites", return_value=sites):
                report = depopulate_hot_lut_sites(
                    root/"input.pl", root/"name_map.json", root/"candidate.pl",
                    mapped_sha256="mapped", packed_sha256="packed", hot_clock_regions=["X6Y9"],
                )
            self.assertEqual(report["counters"]["moved_luts"], 2)
            self.assertTrue(report["requires_exact_placement_validation"])

    def test_invalid_or_missing_region_contract_is_rejected(self):
        def contract(regions):
            return {"coordinate_system": {"clock_regions": {"regions": regions}}}
        for name_map in ({}, contract([None]), contract([
            {"name": "X0Y0", "bbox": [0, 0, 2, 2]},
            {"name": "X1Y0", "bbox": [2, 0, 3, 2]},
        ]), contract([{"name": "X0Y0", "bbox": [True, 0, 2, 2]}])):
            with self.subTest(name_map=name_map), self.assertRaises(ValidationError):
                _hot_region_boxes(name_map, {"X0Y0", "X1Y0"})


if __name__ == "__main__":
    unittest.main()
