"""Actual RTL qualification; skips explicitly when Icarus is unavailable."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ULX3SRTLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iverilog = shutil.which(os.environ.get("IVERILOG", "iverilog"))
        cls.vvp = shutil.which(os.environ.get("VVP", "vvp"))
        if not cls.iverilog or not cls.vvp:
            raise unittest.SkipTest("actual RTL qualification requires iverilog and vvp")

    def simulate(self, name, parameters=(), expected_failure=None, generated=()):
        sources = [ROOT / "rtl/transport" / ("emuflow_gpio_" + n + ".sv")
                   for n in ("uart", "record", "endpoint", "exchange")]
        sources.append(ROOT / "rtl/transport/emuflow_snapshot_host.sv")
        with tempfile.TemporaryDirectory(prefix="ulx3s-rtl-") as scratch:
            output = Path(scratch) / "simulation"
            for index, content in enumerate(generated):
                source = Path(scratch) / f"generated{index}.sv"
                source.write_text(content)
                sources.append(source)
            command = [self.iverilog, "-g2012", "-s", name, "-o", str(output)]
            command += [f"-P{name}.{p}" for p in parameters]
            command += [str(s) for s in sources]
            command += [str(ROOT / "tests/fixtures" / (name + ".sv"))]
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([self.vvp, str(output)], capture_output=True,
                                    text=True, timeout=30)
            text = result.stdout + result.stderr
            if expected_failure:
                self.assertNotEqual(result.returncode, 0, text)
                self.assertIn(expected_failure, text)
            else:
                self.assertEqual(result.returncode, 0, text)
                self.assertIn("PASS", text)

    def test_three_crossing_chain(self):
        self.simulate("ulx3s_rounds_tb")

    def test_insufficient_rounds_detect_stale_value(self):
        self.simulate("ulx3s_rounds_tb", ("ROUND_COUNT=1",),
                      expected_failure="stale combinational chain value")

    def test_protocol_errors(self):
        self.simulate("ulx3s_exchange_errors_tb")

    def test_record_integrity(self):
        self.simulate("ulx3s_record_tb")

    def test_host_request_response_binding(self):
        from test_snapshot_pair import generated_host_pair
        pair = generated_host_pair()
        self.simulate("ulx3s_host_tb", generated=[b["rtl"] for b in pair["boards"].values()])

    def test_host_record_protocol(self):
        self.simulate("ulx3s_host_records_tb")

    def test_physical_host_uart_composition(self):
        from test_snapshot_pair import generated_host_pair
        pair = generated_host_pair()
        self.simulate("ulx3s_host_uart_tb", generated=[b["rtl"] for b in pair["boards"].values()])

    def test_automatically_lowered_three_crossing_partitions(self):
        from emuflow.ir import EmuIR
        from emuflow.snapshot_netlist import emit_snapshot_partition
        from emuflow.snapshot_rounds import derive_snapshot_rounds
        from emuflow.ulx3s_dut import build_ulx3s_snapshot_top

        def ep(cell, port):
            return dict(instance=cell, port=port, bit=0)

        cells = [dict(id=i, type="$_DFF_P_", resources={"ff": 1}) for i in ("a", "b")]
        cells += [dict(id=i, type="LUT1", parameters={"INIT": "01"}, resources={"lut": 1})
                  for i in ("x", "y")]
        nets = [dict(id="clk", cut_class="clock", drivers=[ep(None, "clk")],
                     sinks=[ep("a", "C"), ep("b", "C")])]
        for name, a, p, b, q in (("qa", "a", "Q", "x", "I0"),
                                  ("mid", "x", "O", "y", "I0"),
                                  ("last", "y", "O", "b", "D"),
                                  ("qb", "b", "Q", "a", "D")):
            nets.append(dict(id=name, cut_class="combinational", drivers=[ep(a, p)], sinks=[ep(b, q)]))
        ir = EmuIR(dict(schema="emuflow.emuir/v1", design=dict(name="chain", top="chain", source_format="test"),
                        ports=[dict(id="clk", direction="input", width=1)], clocks=[], instances=cells, nets=nets))
        assignment = {"a": "board0", "b": "board1", "x": "board1", "y": "board0"}
        rounds = derive_snapshot_rounds(ir, assignment, port_owners={})
        self.assertEqual(rounds, 3)
        generated = []
        for board in ("board0", "board1"):
            rtl, interface = emit_snapshot_partition(ir, assignment, board=board,
                module="lowered_"+board, port_owners={}, initial_state={"a": 0, "b": 1})
            self.assertEqual(interface["host_inputs"], [])
            self.assertEqual(interface["host_outputs"], [])
            # No actual host bits exist in this closed semantic fixture. Bind
            # only the lowerer's unused width-one padding port to zero.
            wrapper = f'''module closed_{board}(input wire clk,reset,step,
                input wire [1:0] imported_values,output wire [1:0] exported_values);
                lowered_{board} dut(.clk(clk),.reset(reset),.step(step),
                    .imported_values(imported_values),.exported_values(exported_values),
                    .host_inputs(1'b0),.host_outputs());
                endmodule'''
            generated += [rtl, wrapper, build_ulx3s_snapshot_top(top="lowered_top_"+board,
                dut_module="closed_"+board, board=board, exported_bits=2, imported_bits=2,
                words=interface["words"], session_id=0x452, evaluation_rounds=rounds)]
        self.simulate("ulx3s_lowered_rounds_tb", generated=generated)

    def test_independent_clock_regressions(self):
        for name in ("ulx3s_uart_tb", "ulx3s_endpoint_tb", "ulx3s_exchange_tb"):
            for half_period in ("19.8", "20.2"):
                with self.subTest(name=name, half_period=half_period):
                    self.simulate(name, (f"RX_HALF_PERIOD={half_period}",))
