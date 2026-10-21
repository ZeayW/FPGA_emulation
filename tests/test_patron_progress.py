"""Progress is observable during execution and does not change native results."""
import concurrent.futures
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.partition_pressure import _write_patron_native_input, run_partition_pressure_native
from tests.native_build import patron_refiner


class PatronProgressTest(unittest.TestCase):
    def setUp(self):
        from tests.test_partition_pressure import PartitionPressureTest
        self.fixture = PartitionPressureTest()
        self.fixture.setUp()

    def invoke(self, executable, log, **kwargs):
        f = self.fixture
        return run_partition_pressure_native(f.ir, f.platform, f.clusters, f.constraints,
            f.route_constraints, f.model, f.initial, executable=str(executable),
            stderr_path=log, **kwargs)

    def test_progress_preserves_complete_and_summary_output(self):
        self.check_progress_outputs(self.fixture, 9)

    def test_v14_progress_preserves_static_exact_output(self):
        from emuflow.partition import build_clusters, build_partition_assignment
        from emuflow.partition_pressure import build_partition_pressure_model
        f = self.fixture
        f.clusters = build_clusters(f.ir, f.constraints, cut_mode="static-exact-combinational",
            max_cross_fpga_dependency_depth=2,
            static_exact_candidate_policy="assignment-derived-acyclic-v2")
        f.initial = build_partition_assignment(f.ir, f.platform, f.clusters, f.constraints,
            {c["id"]: ("a" if c["instances"][0] in {"u0", "u2"} else "b")
             for c in f.clusters["clusters"]}, provider="fixture", seed=7)
        f.model = build_partition_pressure_model(f.ir, f.platform, f.clusters,
            f.constraints, f.timing, f.route_constraints)
        self.check_progress_outputs(f, 14)

    def check_progress_outputs(self, f, version):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "patron.in"
            _write_patron_native_input(source, f.platform, f.clusters, f.constraints,
                f.route_constraints, f.model, f.initial, 10, version)
            for flags in ([], ["--summary-output"]):
                outputs = []
                diagnostics = []
                for progress in ([], ["--progress"]):
                    output = root / "patron.out"
                    result = subprocess.run([str(patron_refiner()), *flags, *progress,
                        str(source), str(output)], capture_output=True, text=True, check=True)
                    outputs.append(output.read_bytes())
                    diagnostics.append(result.stderr)
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(diagnostics[0], "\n".join(line for line in diagnostics[1].splitlines()
                    if not line.startswith("PATRON_PROGRESS ")) + ("\n" if diagnostics[0] else ""))
                for stage in ("read", "initialize", "cluster-index", "path-index", "exposure", "sweep", "complete"):
                    self.assertIn("PATRON_PROGRESS phase=" + stage, diagnostics[1])
                self.assertIn("affected_paths=", diagnostics[1])

    def test_python_success_result_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "refiner.log"
            before = self.invoke(patron_refiner(), None, algorithm_version=9)
            after = self.invoke(patron_refiner(), log, algorithm_version=9)
            self.assertEqual(before, after)
            self.assertIn("PATRON_PROGRESS phase=complete", log.read_text())

    def test_python_log_visible_before_exit_and_failure_detail_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable, release, log = root / "fake", root / "release", root / "live.log"
            executable.write_text("#!/bin/sh\necho live-native-diagnostic >&2\n"
                + "while [ ! -f " + shlex.quote(str(release)) + " ]; do sleep 0.02; done\nexit 7\n")
            executable.chmod(0o755)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(self.invoke, executable, log)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if log.exists() and "live-native-diagnostic" in log.read_text():
                            break
                        time.sleep(.01)
                    self.assertIn("live-native-diagnostic", log.read_text())
                    self.assertFalse(future.done())
                finally:
                    release.touch()
                with self.assertRaisesRegex(ValidationError, "exit code 7: live-native-diagnostic"):
                    future.result(timeout=5)

    def test_timeout_preserves_diagnostic_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable, log = root / "fake", root / "timeout.log"
            executable.write_text("#!/bin/sh\necho before-timeout >&2\nexec sleep 5\n")
            executable.chmod(0o755)
            with self.assertRaisesRegex(ValidationError, "exceeded timeout"):
                self.invoke(executable, log, timeout_seconds=1)
            self.assertIn("before-timeout", log.read_text())


if __name__ == "__main__":
    unittest.main()
