from tests.compile_adapter_fixture import preflight as run_preflight
"""Research defaults must reach the selected tools without a VTR fallback."""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.flow.cli import _build_parser, _dispatch
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.flow.multi_fpga_physical_flow import run_multi_fpga_physical_flow


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
                "emuflow.flow.cli." + target, return_value={"status": "pass", "summary": {"status": "pass"}}
            ) as run, patch("emuflow.flow.cli._print_json"):
                args = _build_parser().parse_args(command)
                self.assertEqual(_dispatch(args), 0)
                for name, value in expected.items():
                    self.assertEqual(run.call_args.kwargs[name], value)

    def test_retired_vtr_profile_and_synthesis_command_are_rejected(self):
        from contextlib import redirect_stderr
        import io
        for command in (
            ["multi-fpga", "compile", "design.v", "--out", "unused",
             "--mapping-profile", "vtr-hard-blocks"],
            ["vpr", "synth", "design.v", "--top", "design", "-o", "unused"],
            ["benchmark-matrix-validate", "retired.json"],
        ):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _build_parser().parse_args(command)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.frontend.frontend_native.prepare_frontend"
        ) as frontend, patch("emuflow.flow.multi_fpga_flow.resolve_native_executable") as tool:
            out = Path(temporary) / "flow"
            with self.assertRaisesRegex(EmuFlowError, "unsupported multi-FPGA mapping profile"):
                run_preflight(output_dir=out, mapping_profile="vtr-hard-blocks")
            frontend.assert_not_called()
            tool.assert_not_called()
            self.assertFalse(out.exists())

    def test_retired_physical_routes_fail_before_any_artifact_or_tool(self):
        from contextlib import redirect_stderr
        import io
        commands = (
            ["multi-fpga", "compile", "rtl.v", "--out", "unused", "--physical-backend", "open"],
            ["multi-fpga", "physical", "--split", "unused", "--platform", "unused",
             "--schedule", "unused", "--out", "unused", "--backend", "open"],
            ["multi-fpga", "compile", "rtl.v", "--out", "unused", "--physical-vpr", "vpr"],
            ["vpr", "run"],
            ["arch", "import-vtr"], ["arch", "fetch-default-vtr"],
            ["arch", "validate-vtr"], ["arch", "validate-vtr-timing"],
        )
        for command in commands:
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _build_parser().parse_args(command)
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.flow.multi_fpga_flow.resolve_native_executable",
            side_effect=AssertionError("tool resolution before backend rejection"),
        ), patch(
            "emuflow.flow.multi_fpga_physical_flow._small",
            side_effect=AssertionError("artifact read before backend rejection"),
        ):
            root = Path(temporary)
            with self.assertRaisesRegex(EmuFlowError, "unsupported physical backend"):
                run_preflight(output_dir=root / "compile", physical_backend="open")
            with self.assertRaisesRegex(ValidationError, "physical backend"):
                run_multi_fpga_physical_flow(root / "split", root / "board", root / "schedule",
                    root / "physical", backend="open")
            with self.assertRaisesRegex(TypeError, "vpr"):
                run_multi_fpga_physical_flow(root / "split", root / "board", root / "schedule",
                    root / "physical", vpr="retired")
            self.assertEqual(list(root.iterdir()), [])
        import json
        from emuflow.physical.physical_backend import PHYSICAL_BACKENDS
        descriptor = json.loads((ROOT / "schemas/physical-backend-v1.schema.json").read_text())
        result = json.loads((ROOT / "schemas/physical-partition-result-v1.schema.json").read_text())
        self.assertEqual(set(descriptor["properties"]["id"]["enum"]), set(PHYSICAL_BACKENDS))
        self.assertEqual(set(result["properties"]["identity"]["properties"]["backend"]["enum"]), set(PHYSICAL_BACKENDS))

    def test_explicit_physical_worker_selection_reaches_dispatch(self):
        args = _build_parser().parse_args([
            "multi-fpga", "physical", "--split", "split", "--platform", "board.json",
            "--schedule", "schedule.json", "--out", "worker",
            "--fpga", "fpga2", "--fpga", "fpga1",
        ])
        with patch(
            "emuflow.flow.cli.run_multi_fpga_physical_flow", return_value={"summary": {}}
        ) as run, patch("emuflow.flow.cli._print_json"):
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

    def test_compile_submits_xilinx_policy_without_profile_override(self):
        with patch("emuflow.flow.native_compile.run_native_multi_fpga_flow", return_value={"status": "pass"}) as controller:
            run_multi_fpga_flow(output_dir=Path("out"), sources=[ROOT / "examples/rtl/counter.v"], top="counter", clocks=["clk"], clock_periods={"clk": 10.0})
        self.assertEqual(controller.call_args.kwargs["mapping_profile"], XILINX_PROFILE)
        self.assertEqual(controller.call_args.kwargs["sources"], [ROOT / "examples/rtl/counter.v"])

    def test_retired_generic_upstream_is_rejected_before_any_work(self):
        from contextlib import redirect_stderr
        import io
        import json
        from emuflow.flow.benchmark_inputs import BenchmarkRun

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _build_parser().parse_args([
                "multi-fpga", "compile", "design.v", "--out", "unused",
                "--mapping-profile", "generic-soft",
            ])
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.frontend.frontend_native.prepare_frontend"
        ) as frontend, patch(
            "emuflow.flow.multi_fpga_flow.resolve_native_executable"
        ) as tool:
            out = Path(temporary) / "flow"
            with self.assertRaisesRegex(EmuFlowError, "unsupported multi-FPGA mapping profile"):
                run_preflight(output_dir=out, mapping_profile="generic-soft")
            frontend.assert_not_called()
            tool.assert_not_called()
            self.assertFalse(out.exists())
            value = json.loads((ROOT / "benchmarks/runs/picorv32_l2.json").read_text())
            value["physical_mapping_profile"] = "generic-soft"
            spec = Path(temporary) / "retired.json"
            spec.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValidationError, "physical_mapping_profile"):
                BenchmarkRun.load(spec)

    def test_default_physical_requires_rapidwright_inputs_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "emuflow.flow.multi_fpga_physical_flow._invoke"
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
