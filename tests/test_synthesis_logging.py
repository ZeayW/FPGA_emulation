import sys
import tempfile
import unittest
from pathlib import Path

from emuflow.synthesis import _run_logged_yosys


class SynthesisLoggingTests(unittest.TestCase):
    def test_log_visible_before_subprocess_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthesis.log"
            code = ("import os,sys; os.write(1,b'phase-start\\n'); "
                    "assert open(sys.argv[1],'rb').read()==b'phase-start\\n'; "
                    "os.write(2,b'phase-end\\n')")
            self.assertEqual(_run_logged_yosys([sys.executable, "-c", code, str(path)], path), (0, ""))
            self.assertEqual(path.read_text(), "phase-start\nphase-end\n")

    def test_failure_tail_bounded_and_full_scratch_log_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthesis.log"
            code = "import os; os.write(1,b'x'*200000+b'\\nERROR: failed\\n'); raise SystemExit(7)"
            status, tail = _run_logged_yosys([sys.executable, "-c", code], path)
            self.assertEqual(status, 7)
            self.assertEqual(len(tail), 65536)
            self.assertTrue(tail.endswith("ERROR: failed\n"))
            self.assertGreater(path.stat().st_size, 200000)

    def test_no_log_failure_has_diagnostic(self):
        status, tail = _run_logged_yosys(
            [sys.executable, "-c", "print('bad'); raise SystemExit(2)"], None)
        self.assertEqual((status, tail), (2, "bad\n"))
