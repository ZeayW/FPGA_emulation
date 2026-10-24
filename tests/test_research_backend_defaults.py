"""Research defaults must reach the selected tools without a VTR fallback."""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import _build_parser, _dispatch
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.multi_fpga_physical_flow import run_multi_fpga_physical_flow


ROOT = Path(__file__).resolve().parents[1]
XILINX_PROFILE = "xilinx-ultrascaleplus-open-v1"


class ResearchBackendDefaultsTest(unittest.TestCase):
    def test_cli_defaults_reach_compile_and_physical_dispatch(self):
        cases = [
            (
                ["multi-fpga", "compile", "design.v", "--platform", "board.json", "--out", "out"],
                "run_multi_fpga_flow",
                {"physical_backend": "rapidwright", "mapping_profile": XILINX_PROFILE},
            ),
            (
                ["multi-fpga", "physical", "--split", "split", "--platform", "board.json",
                 "--schedule", "schedule.json", "--out", "out"],
                "run_multi_fpga_physical_flow",
                {"backend": "rapidwright"},
            ),
        ]
        for command, target, expected in cases:
            with self.subTest(command=command[:2]), patch(
                "emuflow.cli." + target, return_value={"status": "pass", "summary": {"status": "pass"}}
            ) as run, patch("emuflow.cli._print_json"):
                args = _build_parser().parse_args(command)
                self.assertEqual(_dispatch(args), 0)
                for name, value in expected.items():
                    self.assertEqual(run.call_args.kwargs[name], value)

    def test_vtr_requires_and_preserves_explicit_selection(self):
        args = _build_parser().parse_args([
            "multi-fpga", "compile", "design.v", "--platform", "board.json",
            "--out", "out", "--physical-backend", "open",
            "--mapping-profile", "vtr-hard-blocks",
        ])
        with patch("emuflow.cli.run_multi_fpga_flow", return_value={"status": "pass"}) as run, patch(
            "emuflow.cli._print_json"
        ):
            self.assertEqual(_dispatch(args), 0)
            self.assertEqual(run.call_args.kwargs["physical_backend"], "open")
            self.assertEqual(run.call_args.kwargs["mapping_profile"], "vtr-hard-blocks")
        physical = _build_parser().parse_args([
            "multi-fpga", "physical", "--split", "split", "--platform", "board.json",
            "--schedule", "schedule.json", "--out", "out", "--backend", "open",
        ])
        self.assertEqual(physical.backend, "open")

    def test_explicit_physical_worker_selection_reaches_dispatch(self):
        args = _build_parser().parse_args([
            "multi-fpga", "physical", "--split", "split", "--platform", "board.json",
            "--schedule", "schedule.json", "--out", "worker",
            "--fpga", "fpga2", "--fpga", "fpga1",
        ])
        with patch(
            "emuflow.cli.run_multi_fpga_physical_flow", return_value={"summary": {}}
        ) as run, patch("emuflow.cli._print_json"):
            self.assertEqual(_dispatch(args), 0)
        self.assertEqual(run.call_args.kwargs["fpga_ids"], ["fpga2", "fpga1"])

    def test_python_api_defaults_match_cli(self):
        for function, parameter, expected in [
            (run_multi_fpga_flow, "mapping_profile", XILINX_PROFILE),
            (run_multi_fpga_flow, "physical_backend", "rapidwright"),
            (run_multi_fpga_physical_flow, "backend", "rapidwright"),
        ]:
            with self.subTest(function=function.__name__, parameter=parameter):
                self.assertEqual(inspect.signature(function).parameters[parameter].default, expected)

    def test_compile_uses_xilinx_synthesis_without_profile_override(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.multi_fpga_flow.run_xilinx_ultrascaleplus_yosys_stream",
            side_effect=EmuFlowError("selected Xilinx synthesis"),
        ) as xilinx, patch("emuflow.multi_fpga_flow.run_vtr_yosys") as vtr, patch("emuflow.multi_fpga_flow._validate_mapping_timing_contract"), patch("emuflow.multi_fpga_flow.check_opensta_engine_version"), patch("emuflow.multi_fpga_flow.resolve_native_executable", return_value="/usr/bin/true"):
            with self.assertRaisesRegex(EmuFlowError, "selected Xilinx synthesis"):
                run_multi_fpga_flow(
                    ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json",
                    Path(temporary) / "flow",
                    sources=[ROOT / "examples/rtl/counter.v"],
                    top="counter", clocks=["clk"], clock_periods={"clk": 10.0},
                )
            xilinx.assert_called_once()
            vtr.assert_not_called()


    def test_retired_generic_upstream_is_rejected_before_any_work(self):
        from contextlib import redirect_stderr
        import io
        import json
        from emuflow.benchmark import BenchmarkRun

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _build_parser().parse_args([
                "multi-fpga", "compile", "design.v", "--out", "unused",
                "--mapping-profile", "generic-soft",
            ])
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.multi_fpga_flow.run_phase1"
        ) as frontend, patch(
            "emuflow.multi_fpga_flow.resolve_native_executable"
        ) as tool:
            out = Path(temporary) / "flow"
            with self.assertRaisesRegex(EmuFlowError, "unsupported multi-FPGA mapping profile"):
                run_multi_fpga_flow(output_dir=out, mapping_profile="generic-soft")
            frontend.assert_not_called()
            tool.assert_not_called()
            self.assertFalse(out.exists())
            value = json.loads((ROOT / "benchmarks/runs/koios_dla_medium_l5.json").read_text())
            value["physical_mapping_profile"] = "generic-soft"
            spec = Path(temporary) / "retired.json"
            spec.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValidationError, "physical_mapping_profile"):
                BenchmarkRun.load(spec)

    def test_default_physical_requires_rapidwright_inputs_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.multi_fpga_physical_flow.run_vpr_route_packed"
        ) as vpr:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "openparf-native requires source-sealed"):
                run_multi_fpga_physical_flow(
                    root / "split", root / "board.json", root / "schedule.json", root / "out"
                )
            vpr.assert_not_called()
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
