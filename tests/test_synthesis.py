import unittest
from pathlib import Path

from emuflow.errors import EmuFlowError
from emuflow.synthesis import build_generic_yosys_script, build_yosys_script
from emuflow.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE


class SynthesisTest(unittest.TestCase):
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
        for option in ("-nocarry", "-nodsp", "-nobram"):
            self.assertNotIn(option, script)

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
        self.assertIn('-I"rtl/include"', script)
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
        )
        self.assertIn("abc -lut 6", script)
        self.assertIn("memory_map", script)
        self.assertIn('write_json "build/counter-generic.json"', script)
        self.assertNotIn("synth_xilinx", script)
        self.assertNotIn("xcup", script)

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
