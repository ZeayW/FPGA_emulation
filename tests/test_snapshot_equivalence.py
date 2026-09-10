import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.snapshot_equivalence import build_snapshot_equivalence_testbench
from test_snapshot_pair import host_fixture, generated_host_pair


class SnapshotEquivalenceTests(unittest.TestCase):
    def generate(self, vectors=None):
        ir, assignment = host_fixture()
        return build_snapshot_equivalence_testbench(ir, assignment, generated_host_pair(),
            initial_state={"q": 0}, vectors=vectors if vectors is not None else [{"in": v} for v in (0, 1, 1, 0)])

    def test_input_coverage_and_scope(self):
        tb, summary = self.generate()
        self.assertEqual(summary["observed_ff"], 1)
        self.assertEqual(summary["macrocycles"], 4)
        self.assertFalse(summary["physical_timing_proof"])
        self.assertIn("b.dut.state0", tb)
        for vectors in ([], [{}], [{"in": 2}], [{"in": True}], [{"extra": 0}]):
            with self.assertRaises(ValidationError): self.generate(vectors)

    def test_actual_uart_rtl_and_corrupted_state_rejection(self):
        compiler = shutil.which(os.environ.get("IVERILOG", "iverilog"))
        runtime = shutil.which(os.environ.get("VVP", "vvp"))
        if not compiler or not runtime: self.skipTest("actual Icarus tools required")
        root = Path(__file__).resolve().parents[1]
        sources = list((root / "rtl/transport").glob("emuflow_gpio_*.sv"))
        sources.append(root / "rtl/transport/emuflow_snapshot_host.sv")
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                pair = generated_host_pair()
                if corrupt:
                    original = pair["boards"]["board1"]["rtl"]
                    self.assertIn("else if(step) state0<=", original)
                    pair["boards"]["board1"]["rtl"] = original.replace("else if(step) state0<=", "else if(step) state0<=~")
                files=[]
                for name, board in pair["boards"].items():
                    p=out/(name+".sv"); p.write_text(board["rtl"]); files.append(p)
                tb=out/"tb.sv"; tb.write_text(self.generate()[0]); files.append(tb)
                image=out/"sim"
                compile_result=subprocess.run([compiler,"-g2012","-s","snapshot_equivalence_tb","-o",str(image),*[str(p) for p in sources+files]],capture_output=True,text=True,timeout=30)
                self.assertEqual(compile_result.returncode,0,compile_result.stderr)
                result=subprocess.run([runtime,str(image)],capture_output=True,text=True,timeout=30)
                if corrupt:
                    self.assertNotEqual(result.returncode,0)
                    self.assertIn("state mismatch",result.stdout)
                else:
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertIn("PASS snapshot macrocycles=4 observed_ff=1",result.stdout)
