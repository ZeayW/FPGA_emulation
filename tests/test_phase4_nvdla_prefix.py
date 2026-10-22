"""The prefix supervisor retains failures and kills only its own worker group."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("nvdla_prefix_test_runner", ROOT / "scripts/phase4_nvdla_prefix.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PrefixSupervisorTest(unittest.TestCase):
    def inputs(self, root):
        binaries = root / "native/bin"
        binaries.mkdir(parents=True)
        for name in ("yosys", "sta", "openroad", "emuflow_patron_refiner", "emuflow_tritonpart_balance_legalizer"):
            path = binaries / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        board = root / "board.json"
        runner.write(board, {})
        return runner.parser().parse_args([
            "--benchmark", str(root / "benchmark.json"), "--source-root", str(root),
            "--platform", str(board), "--architecture-timing-db", str(root / "timing.json"),
            "--output", str(root / "run"), "--yosys", str(binaries / "yosys"),
            "--sta", str(binaries / "sta"), "--native-root", str(root / "native"),
        ])

    def test_worker_seals_with_the_same_architecture_timing_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.inputs(Path(temporary))
            args.output.mkdir()
            runner.write(args.output / "provenance.json", {})
            with patch.object(runner, "verify_provenance"), \
                    patch.object(runner.BenchmarkRun, "load"), \
                    patch.object(runner, "flow_options", return_value={}), \
                    patch("emuflow.multi_fpga_flow.run_multi_fpga_flow",
                          return_value={"completed_through": "phase3"}), \
                    patch.object(runner, "seal_contract", return_value={}) as seal:
                runner.worker(args)
            seal.assert_called_once_with(args.output / "common", args.platform,
                args.output / "provenance.json",
                architecture_timing_db=args.architecture_timing_db)

    def test_nonzero_worker_is_not_published_as_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.inputs(Path(temporary))
            child = Mock(pid=123, returncode=7)
            child.poll.return_value = 7
            with patch.object(runner, "check_platform"), patch.object(runner.BenchmarkRun, "load"), \
                    patch.object(runner, "capture_provenance", return_value={}), \
                    patch.object(runner.subprocess, "Popen", return_value=child), \
                    patch.object(runner.os, "killpg") as kill:
                with self.assertRaisesRegex(ValueError, "worker failed"):
                    runner.supervise(args, [])
                kill.assert_not_called()
            record = runner.read(args.output / "process.json")
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["exit_code"], 7)
            self.assertIsNone(record["peak_sampled_tree_rss_kib"])
            self.assertFalse((args.output / "contract.json").exists())

    def test_memory_guard_stops_only_worker_group_and_retains_peak(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.inputs(Path(temporary))
            child = Mock(pid=456, returncode=-9)
            child.poll.return_value = None
            rss = 101 * 1024**2
            with patch.object(runner, "check_platform"), patch.object(runner.BenchmarkRun, "load"), \
                    patch.object(runner, "capture_provenance", return_value={}), \
                    patch.object(runner.subprocess, "Popen", return_value=child), \
                    patch.object(runner, "process_rss", return_value=rss), \
                    patch.object(runner.os, "killpg") as kill:
                with self.assertRaisesRegex(ValueError, "memory guard"):
                    runner.supervise(args, [])
                kill.assert_called_once_with(456, runner.signal.SIGKILL)
                child.wait.assert_called_once()
            record = runner.read(args.output / "process.json")
            self.assertEqual(record["peak_sampled_tree_rss_kib"], rss)
            self.assertEqual(record["status"], "failed")
            self.assertIn("not a hard cap", record["memory_measurement"])


if __name__ == "__main__":
    unittest.main()
