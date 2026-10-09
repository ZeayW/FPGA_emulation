import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import _build_parser, main
from emuflow.errors import EmuFlowError
from emuflow.io import read_json
from emuflow.synthesis import (
    build_generic_yosys_script,
    build_yosys_script,
    run_generic_yosys,
    run_yosys,
)
from emuflow.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE


class SynthesisTest(unittest.TestCase):
    def test_xilinx_json_can_stream_directly_to_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "design.v"
            source.write_text("module design; endmodule\n", encoding="utf-8")
            executable = root / "fake-yosys"
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    sys.stderr.write("diagnostic tail\\n")
                    json.dump({"modules": {"design": {"cells": {}}}}, sys.stdout)
                    """
                ),
                encoding="utf-8",
            )
            executable.chmod(0o755)
            output = root / "mapped.json.gz"
            log = root / "yosys.log"

            run_yosys(
                [source],
                "design",
                output,
                executable=str(executable),
                log_path=log,
                mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            )

            self.assertEqual(
                read_json(output),
                {"modules": {"design": {"cells": {}}}},
            )
            self.assertEqual(log.read_text(encoding="utf-8"), "diagnostic tail\n")

    def test_production_yosys_invocations_are_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "design.v"
            source.write_text("module design; endmodule\n", encoding="utf-8")
            captured = []

            def complete(command):
                captured.append(command)
                output = command[-1].split('write_json "', 1)[1].split('"', 1)[0]
                Path(output).write_text("{}\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="")

            with patch(
                "emuflow.synthesis.resolve_native_executable",
                return_value="yosys",
            ), patch(
                "emuflow.synthesis.run_with_bounded_output",
                side_effect=complete,
            ):
                run_generic_yosys([source], "design", root / "generic.json")
                run_yosys([source], "design", root / "mapped.json")

        self.assertEqual(
            [command[1:3] for command in captured],
            [["-q", "-p"], ["-q", "-p"]],
        )

    def test_cli_exposes_fail_closed_mapping_profile(self) -> None:
        args = _build_parser().parse_args([
            "synth-yosys",
            "rtl/counter.sv",
            "--top", "counter",
            "--output", "build/counter.json",
            "--mapping-profile", XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        ])
        self.assertEqual(args.mapping_profile, XILINX_ULTRASCALEPLUS_OPEN_PROFILE)

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

    def test_cli_mapping_profile_runs_normalizing_wrapper(self) -> None:
        with patch("emuflow.cli.run_xilinx_ultrascaleplus_yosys") as run:
            run.return_value = {
                "status": "pass",
                "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            }
            status = main([
                "synth-yosys",
                "rtl/counter.sv",
                "--top", "counter",
                "--output", "build/counter.json",
                "--mapping-profile", XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            ])
        self.assertEqual(status, 0)
        run.assert_called_once()

    def test_xcup_script_is_board_independent(self) -> None:
        script = build_yosys_script(
            [Path("rtl/counter.sv")],
            top="counter",
            output=Path("build/counter.json"),
            family="xcup",
        )
        self.assertIn("read_verilog -sv", script)
        self.assertIn("synth_xilinx -family xcup", script)
        self.assertIn("-top counter", script)
        self.assertNotIn('-top "counter"', script)
        self.assertIn("-noiopad -noclkbuf", script)
        self.assertIn("; flatten; opt_clean; check;", script)
        self.assertIn('write_json "build/counter.json"', script)
        self.assertIn("delete t:$scopeinfo", script)

    def test_logic_only_policy_disables_hard_mapping(self) -> None:
        script = build_yosys_script(
            [Path("rtl/design.v")],
            top="design",
            output=Path("build/design.json"),
            policy="logic-only",
        )
        for option in (
            "-nocarry",
            "-nowidelut",
            "-nodsp",
            "-nobram",
            "-nolutram",
            "-nosrl",
        ):
            self.assertIn(option, script)
        self.assertIn("techmap -map", script)
        self.assertIn("logic_only_map.v", script)

    def test_route_a_profile_preserves_declared_hard_blocks(self) -> None:
        script = build_yosys_script(
            [Path("rtl/design.v")],
            top="design",
            output=Path("build/design.json"),
            family="xcup",
            policy="native",
            mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        )
        for option in ("-uram", "-nolutram", "-nosrl"):
            self.assertIn(option, script)
        self.assertIn("-run begin:check", script)
        self.assertIn("blackbox =A:whitebox", script)
        self.assertIn("; flatten; check;", script)
        self.assertNotIn("; flatten; opt_clean;", script)
        self.assertIn(
            'write_json -no-hidden-netnames -no-source-attributes '
            '"build/design.json"',
            script,
        )
        for option in ("-nocarry", "-nodsp", "-nobram"):
            self.assertNotIn(option, script)

    def test_route_a_streaming_script_uses_stdout(self) -> None:
        script = build_yosys_script(
            [Path("rtl/design.v")],
            top="design",
            output=Path("build/design.json.gz"),
            family="xcup",
            policy="native",
            mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            stream_json=True,
        )
        self.assertTrue(
            script.endswith("write_json -no-hidden-netnames -no-source-attributes")
        )
        self.assertNotIn("design.json.gz", script)

    def test_route_a_profile_rejects_incompatible_family(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "requires family"):
            build_yosys_script(
                [Path("rtl/design.v")],
                top="design",
                output=Path("build/design.json"),
                family="xc7",
                mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            )

    def test_include_directories_and_defines_are_explicit(self) -> None:
        script = build_yosys_script(
            [Path("rtl/design.v")],
            top="design",
            output=Path("build/design.json"),
            include_dirs=[Path("rtl/include")],
            defines=["SYNTHESIS", "WIDTH=32"],
        )
        self.assertIn("-Irtl/include", script)
        self.assertNotIn('-I"rtl/include"', script)
        self.assertIn("-DSYNTHESIS", script)
        self.assertIn("-DWIDTH=32", script)

    def test_unsafe_define_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "Yosys define"):
            build_yosys_script(
                [Path("rtl/design.v")],
                top="design",
                output=Path("build/design.json"),
                defines=["SAFE; delete"],
            )

    def test_generic_script_has_no_vendor_family_dependency(self) -> None:
        script = build_generic_yosys_script(
            [Path("rtl/counter.sv")],
            top="counter",
            output=Path("build/counter-generic.json"),
            include_dirs=[Path("rtl/include")],
            defines=["SYNTHESIS"],
        )
        self.assertIn("abc -lut 6", script)
        self.assertIn("memory_map", script)
        self.assertIn('write_json "build/counter-generic.json"', script)
        self.assertNotIn("synth_xilinx", script)
        self.assertNotIn("xcup", script)
        self.assertIn("-Irtl/include", script)
        self.assertIn("-DSYNTHESIS", script)

    def test_include_directory_with_unsafe_yosys_token_characters_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "include directory"):
            build_yosys_script(
                [Path("rtl/design.v")],
                top="design",
                output=Path("build/design.json"),
                include_dirs=[Path("rtl/include with spaces")],
            )

    def test_optional_mapped_verilog_preserves_names(self) -> None:
        script = build_yosys_script(
            [Path("rtl/design.v")],
            top="design",
            output=Path("build/design.json"),
            verilog_output=Path("build/design.v"),
        )
        self.assertIn('setattr -set KEEP "yes" c:*', script)
        self.assertIn('setattr -set DONT_TOUCH "yes" c:*', script)
        self.assertIn('write_verilog -norename "build/design.v"', script)
        self.assertNotIn("write_verilog -noattr", script)

    def test_unknown_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "synthesis policy"):
            build_yosys_script(
                [Path("rtl/design.v")],
                top="design",
                output=Path("build/design.json"),
                policy="magic",
            )

    def test_missing_sources_are_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "at least one RTL source"):
            build_yosys_script(
                [],
                top="counter",
                output=Path("build/counter.json"),
            )

    def test_unsafe_top_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "simple Verilog module name"):
            build_yosys_script(
                [Path("rtl/counter.sv")],
                top="counter; delete",
                output=Path("build/counter.json"),
            )


if __name__ == "__main__":
    unittest.main()
