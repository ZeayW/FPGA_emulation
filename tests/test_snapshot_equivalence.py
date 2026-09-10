import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.snapshot_equivalence import build_snapshot_equivalence_testbench
from emuflow.snapshot_pair import emit_snapshot_pair
from test_snapshot_pair import host_fixture, generated_host_pair


class SnapshotEquivalenceTests(unittest.TestCase):
    def generate(self, vectors=None, timing_events=False):
        ir, assignment = host_fixture()
        return build_snapshot_equivalence_testbench(ir, assignment, generated_host_pair(),
            initial_state={"q": 0}, vectors=vectors if vectors is not None else [{"in": v} for v in (0, 1, 1, 0)],timing_events=timing_events)

    def test_input_coverage_and_scope(self):
        tb, summary = self.generate()
        self.assertEqual(summary["observed_ff"], 1)
        self.assertEqual(summary["macrocycles"], 4)
        self.assertFalse(summary["physical_timing_proof"])
        self.assertIn("b.dut.state0", tb)
        self.assertNotIn('SNAPSHOT_EVENT',tb)
        event_tb,scope=self.generate(timing_events=True)
        self.assertIn('SNAPSHOT_EVENT board1',event_tb)
        self.assertEqual(scope['timing_event_scope'],'simulated_protocol_events_not_measured_link')
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
                tb=out/"tb.sv"; tb.write_text(self.generate(timing_events=True)[0]); files.append(tb)
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
                    events=[x.split() for x in result.stdout.splitlines() if x.startswith('SNAPSHOT_EVENT ')]
                    for board in ('board0','board1'):
                        commits=[x for x in events if x[1]==board and x[3]=='commit']
                        self.assertEqual(len(commits),4)
                        for epoch in range(4):
                            captures=[x for x in events if x[1]==board and x[3]=='tx_capture' and int(x[4])==epoch]
                            updates=[x for x in events if x[1]==board and x[3]=='rx_update' and int(x[4])==epoch]
                            self.assertEqual(len(captures),1)
                            self.assertEqual(len(updates),1)
                            self.assertLess(float(updates[0][2]),float(commits[epoch][2]))

    def test_two_round_event_identity_and_overwritten_capture(self):
        compiler = shutil.which(os.environ.get("IVERILOG", "iverilog"))
        runtime = shutil.which(os.environ.get("VVP", "vvp"))
        if not compiler or not runtime:
            self.skipTest("actual Icarus tools required")
        ir, _ = host_fixture()
        assignment = {"q": "board0", "x": "board1"}
        pair = emit_snapshot_pair(ir, assignment, prefix="two_round",
            port_owners={"in": "board0", "out": "board0"},
            initial_state={"q": 0}, session_id=0x789)
        self.assertEqual(pair["evaluation_rounds"], 2)
        tb, _ = build_snapshot_equivalence_testbench(ir, assignment, pair,
            initial_state={"q": 0}, vectors=[{"in": 1}, {"in": 0}], timing_events=True)
        root = Path(__file__).resolve().parents[1]
        sources = list((root / "rtl/transport").glob("emuflow_gpio_*.sv"))
        sources.append(root / "rtl/transport/emuflow_snapshot_host.sv")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for name, board in pair["boards"].items():
                path = out / (name + ".sv")
                path.write_text(board["rtl"])
                sources.append(path)
            path = out / "tb.sv"
            path.write_text(tb)
            sources.append(path)
            image = out / "sim"
            compiled = subprocess.run([compiler, "-g2012", "-s", "snapshot_equivalence_tb",
                "-o", str(image), *map(str, sources)], capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            run = subprocess.run([runtime, str(image)], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        events = [line.split() for line in run.stdout.splitlines() if line.startswith("SNAPSHOT_EVENT ")]
        def selected(board, kind, epoch):
            return [row for row in events if row[1] == board and row[3] == kind and int(row[4]) == epoch]
        for board, peer in (("board0", "board1"), ("board1", "board0")):
            for epoch in range(4):
                captures = selected(board, "tx_capture", epoch)
                # Follower captures speculatively in FINISH, then overwrites
                # at PREPARE. Bind transmitted data to the LAST capture, not
                # the first event and not an assumed one-capture-per-epoch.
                self.assertEqual(len(captures), 2 if board == "board1" and epoch % 2 else 1)
                tx = selected(board, "tx_data", epoch)
                rx = selected(peer, "rx_update", epoch)
                self.assertEqual(len(tx), 1)
                self.assertEqual(len(rx), 1)
                self.assertLess(float(captures[-1][2]), float(tx[0][2]))
                self.assertLess(float(tx[0][2]), float(rx[0][2]))
                self.assertTrue(all(int(row[5]) == epoch % 2 for row in captures + tx + rx))
                commit = selected(board, "commit", epoch)
                self.assertEqual(len(commit), epoch % 2)
                if commit:
                    self.assertLess(float(selected(board, "rx_update", epoch)[0][2]), float(commit[0][2]))
