"""Regression cases from the NVDLA multi-resource partition failure."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ResourceBoundsTest(unittest.TestCase):
    def test_componentwise_comparison_on_captured_resource_vectors(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "bounds.cpp"
            source.write_text(r'''
#include "ResourceBalance.h"
#include <cassert>
#include <limits>
int main() {
  using par::ResourceWithin;
  using par::ResourceBalanced;
  using Row = std::vector<float>;
  using Matrix = std::vector<Row>;
  const Row upper{1083876,821795,251251,312,128,8056};
  // These coarse vertices have legal cell counts but cannot fit any FPGA.
  assert(!ResourceWithin(Row{919665,642669,263486,336,236,10093}, upper));
  assert(!ResourceWithin(Row{745912,507790,227979,154,142,7474}, upper));
  assert(!ResourceWithin(Row{394441,285075,104197,480,11,3922}, upper));
  // The old final checker claimed balance=true for this assignment.
  const Matrix loads{{942229,828129,105831,0,11,6456},
                     {1014926,836190,172667,613,78,4380},
                     {983020,603406,368539,186,142,8031},
                     {1001194,720622,266605,336,236,10429}};
  assert(!ResourceBalanced(loads, Matrix(4, Row(6, 0)), Matrix(4, upper)));
  // Equal components and zero resource weights are legal, even when only
  // some components equal their bound. Shape mismatches fail closed.
  assert(ResourceWithin(Row{9,10,0}, Row{10,10,0}));
  assert(ResourceWithin(upper, upper));
  assert(!ResourceWithin(Row{9,11}, Row{10,10}));
  assert(!ResourceWithin(Row{11,9}, Row{10,10}));
  assert(!ResourceWithin(Matrix{{0}}, Matrix{{0},{0}}));
  assert(!ResourceWithin(Row{0}, Row{0,0}));
  assert(!ResourceWithin(Row{std::numeric_limits<float>::quiet_NaN()}, Row{1}));
  assert(!ResourceBalanced(Matrix{{1,4}}, Matrix{{0,5}}, Matrix{{2,6}}));
}
''')
            binary = root / "bounds"
            subprocess.run([compiler, "-std=c++17", "-I",
                            str(ROOT / "engines/openroad/src/par/src"),
                            str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    @unittest.skipUnless(os.environ.get("EMUFLOW_TEST_OPENROAD"),
                         "requires the rebuilt in-tree OpenROAD")
    def test_native_exact_bounds_and_failure_without_solution_file(self):
        executable = os.environ["EMUFLOW_TEST_OPENROAD"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = root / "input.hgr"
            # One scarce resource is deliberately concentrated in part 0.
            graph.write_text("3 4 11\n1 1 2\n1 2 3\n1 3 4\n"
                             "1 2\n1 0\n1 0\n1 0\n")
            bounds = root / "bounds.txt"
            bounds.write_text("2 2\n0 2 0 2\n0 2 0 0\n")
            script = root / "run.tcl"
            script.write_text(
                f"triton_part_hypergraph -hypergraph_file {{{graph}}} "
                f"-resource_bounds_file {{{bounds}}} -num_parts 2 "
                "-balance_constraint 50 -vertex_dimension 2 "
                "-min_num_vertices_each_part 1 -seed 0\nexit\n")
            result = subprocess.run([executable, "-exit", str(script)],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            solution = Path(f"{graph}.part.2")
            parts = list(map(int, solution.read_text().split()))
            self.assertEqual(parts[0], 0)
            self.assertEqual(parts.count(0), 2)
            self.assertEqual(parts.count(1), 2)
            solution.unlink()

            # ILP must receive fixed placements as constraints, including the
            # symmetric case where resource limits alone cannot select labels.
            graph.write_text("3 4 11\n1 1 2\n1 2 3\n1 3 4\n"
                             "1 0\n1 0\n1 0\n1 0\n")
            fixed = root / "fixed.txt"
            fixed.write_text("1\n1\n0\n0\n")
            bounds.write_text("2 2\n0 2 0 0\n0 2 0 0\n")
            script.write_text(script.read_text().replace(
                "triton_part_hypergraph",
                f"triton_part_hypergraph -fixed_file {{{fixed}}}", 1))
            result = subprocess.run([executable, "-exit", str(script)],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(map(int, solution.read_text().split())),
                             [1, 1, 0, 0])
            solution.unlink()
            bounds.write_text("2 2\n0 1 0 2\n0 1 0 0\n")
            result = subprocess.run([executable, "-exit", str(script)],
                                    capture_output=True, text=True, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(solution.exists())
