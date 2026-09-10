import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from emuflow.ecp5_backend import run_ulx3s_physical
from emuflow.ecp5_qualification import ECP5_85F_CAPACITY
from emuflow.errors import ValidationError


class ECP5BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.rtl = self.root / "dut.sv"
        self.rtl.write_text("module dut; endmodule\n")
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("yosys", "nextpnr-ecp5", "ecppack"):
            p = self.tools / name; p.write_text("test-only mock"); p.chmod(0o700)
        self.out = self.root / "out"

    def run_backend(self, **kwargs):
        return run_ulx3s_physical([self.rtl], top="dut", tools=self.tools,
                                  output_dir=self.out, **kwargs)

    def tool(self, command, **kwargs):
        self.assertEqual(kwargs["cwd"], self.out)
        self.assertEqual(kwargs["env"]["TMPDIR"], str(self.out / "tmp"))
        name = Path(command[0]).name
        if name=="yosys": (self.out / "mapped.json").write_text("{}")
        if name=="nextpnr-ecp5":
            self.assertIn("--85k", command)
            self.assertNotIn("--timing-allow-fail", command)
            self.assertNotIn("--lpf-allow-unconstrained", command)
            (self.out / "routed.config").write_text("mock")
            (self.out / "physical.json").write_text(json.dumps({
                "utilization": {k:{"used":1,"available":v} for k,v in ECP5_85F_CAPACITY.items()},
                "fmax":{"clk":{"achieved":80,"constraint":25}},
                "critical_paths":["large scratch-only diagnostic"]}))
        if name=="ecppack": (self.out / "endpoint.bit").write_bytes(b"mock")
        return SimpleNamespace(returncode=0)

    def test_compact_report_and_tools(self):
        with patch("emuflow.ecp5_backend.subprocess.run", side_effect=self.tool) as run:
            result = self.run_backend(board="board1")
        self.assertEqual(run.call_count,3)
        self.assertEqual(result["board"], "board1")
        self.assertEqual(result["local_qualification"]["status"],"pass")
        self.assertNotIn("critical_paths",result["physical"])
        self.assertIsNone(result["global_wns_tns"])
        self.assertEqual(len(result["sources"][0]["sha256"]),64)

    def test_tool_failure_stops_and_records(self):
        with patch("emuflow.ecp5_backend.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(RuntimeError): self.run_backend()
        self.assertEqual(run.call_count,1)
        self.assertEqual(json.loads((self.out/"summary.json").read_text())["status"],"failed")

    def test_success_exit_without_outputs_rejected(self):
        with patch("emuflow.ecp5_backend.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(ValidationError): self.run_backend()

    def test_invalid_input_does_not_create_run(self):
        with self.assertRaises(ValidationError): self.run_backend(board="unknown")
        self.assertFalse(self.out.exists())
        (self.tools/"yosys").unlink()
        with self.assertRaises(ValidationError): self.run_backend()
        self.assertFalse(self.out.exists())

    def test_no_overwrite(self):
        self.out.mkdir()
        with self.assertRaises(FileExistsError): self.run_backend()

    def test_host_pin_overlay_is_explicit(self):
        with patch("emuflow.ecp5_backend.subprocess.run", side_effect=self.tool):
            result=self.run_backend(host_uart=True)
        self.assertTrue(result["host_uart"])
        self.assertIn('"host_tx" SITE "L4"',(self.out/"endpoint.lpf").read_text())
