"""Retained statistics tooling and retired synthesis-entry contracts."""
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from emuflow.flow.cli import _build_parser
from emuflow.common.errors import EmuFlowError
from emuflow_tools.yosys_mapping_statistics import (
    build_xilinx_mapping_statistics_script,run_xilinx_mapping_statistics)


class SynthesisTest(unittest.TestCase):
    def test_mapping_profile_requires_native_pass_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "design.v"
            source.write_text("module design; endmodule\n")
            output = root / "stats.json"
            footer = "Time spent:\n  80%  2 calls 12.500 sec abc9_exe\n  20%  5 calls 3.125 sec opt\n"

            def complete(command):
                self.assertEqual(command[1:4], ["-v", "2", "-d"])
                self.assertNotIn("-t", command[1:-1])
                output.write_text('{"modules":{"design":{}}}')
                return subprocess.CompletedProcess(command, 0, stdout=footer)

            with patch("emuflow_tools.yosys_mapping_statistics.resolve_native_executable", return_value="yosys"), patch(
                "emuflow_tools.yosys_mapping_statistics.run_with_bounded_output", side_effect=complete
            ):
                report = run_xilinx_mapping_statistics(
                    [source], "design", output, profile_passes=True
                )
                self.assertEqual(report["pass_timings"][0], {
                    "pass": "abc9_exe", "calls": 2, "seconds": 12.5, "percent": 80
                })
                footer = "No timing footer\n"
                with self.assertRaisesRegex(EmuFlowError, "did not emit"):
                    run_xilinx_mapping_statistics(
                        [source], "design", output, profile_passes=True
                    )

    def test_multi_fpga_cli_exposes_shared_compilation_context(self) -> None:
        args = _build_parser().parse_args([
            "multi-fpga", "compile", "rtl/top.v",
            "--top", "top",
            "--platform", "platform.json",
            "--out", "build/flow",
            "--include-dir", "rtl/include",
            "--define", "SYNTHESIS",
        ])
        self.assertEqual(args.include_dir, [Path("rtl/include")])
        self.assertEqual(args.define, ["SYNTHESIS"])

    def test_mapping_statistics_script_writes_only_compact_stats(self) -> None:
        script = build_xilinx_mapping_statistics_script(
            [Path("rtl/design.v")],
            top="design",
            output=Path("build/statistics.json"),
            mapping_strategy="flatten-abc9-v1",
        )
        self.assertIn("-flatten -abc9", script)
        self.assertIn(
            'tee -q -o "build/statistics.json" stat -json -top design',
            script,
        )
        self.assertNotIn("write_json", script)

    def test_retired_python_synthesis_entry_is_absent(self):
        from importlib.util import find_spec
        self.assertIsNone(find_spec("emuflow.frontend.synthesis"))
        with self.assertRaises(SystemExit) as rejected:
            _build_parser().parse_args(["synth-yosys", "unused.v", "--top", "top", "--output", "unused.json"])
        self.assertEqual(rejected.exception.code, 2)

    def test_replaced_python_normalization_and_macro_producers_are_absent(self):
        from importlib.util import find_spec
        from emuflow.physical import xilinx_primitives
        self.assertIsNone(find_spec("emuflow.physical.xilinx_physical_macros"))
        for name in ("normalize_xilinx_mapped_json", "normalize_xilinx_indexed_staging", "audit_xilinx_mapped_json", "_IndexedCellMapping"):
            self.assertFalse(hasattr(xilinx_primitives, name))
