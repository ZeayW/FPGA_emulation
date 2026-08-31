"""Compile the retained OpenROAD readline block with the feature on and off."""

from pathlib import Path
import shutil
import subprocess
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]


class OpenROADReadlineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = shutil.which("c++")
        if self.compiler is None:
            self.skipTest("C++ compiler is unavailable")
        source = (REPOSITORY / "engines/openroad/src/Main.cc").read_text()
        start = source.index("#ifdef ENABLE_READLINE\nstatic int tclReadlineInit")
        end = source.index("// Tcl init executed inside Tcl_Main.", start)
        self.block = source[start:end]

    def compile_block(self, source: str) -> None:
        result = subprocess.run(
            [self.compiler, "-std=c++17", "-fsyntax-only", "-x", "c++", "-"],
            input=source,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disabled_readline_needs_no_optional_headers_or_macros(self) -> None:
        # The default source-complete build has neither tclreadline headers
        # nor TCLRL_VERSION_STR. Every readline-only helper must disappear.
        self.compile_block(self.block)

    def test_enabled_readline_retains_script_discovery(self) -> None:
        # Minimal Tcl declarations isolate compilation from host Tcl packages;
        # the actual helper body is extracted unchanged from the vendor source.
        declarations = """
#include <array>
#include <iostream>
#include <string>
#define ENABLE_READLINE 1
#define TCLRL_VERSION_STR "2.3.8"
struct Tcl_Interp;
int Tcl_Eval(Tcl_Interp*, const char*);
const char* Tcl_GetStringResult(Tcl_Interp*);
constexpr int TCL_OK = 0;
constexpr int TCL_ERROR = 1;
"""
        self.compile_block(
            declarations + self.block
            + "\nvoid probe(Tcl_Interp* interp) {\n"
            + "  (void)tclReadlineInit(interp);\n"
            + "  (void)findPathToTclreadlineInit(interp);\n}\n"
        )


if __name__ == "__main__":
    unittest.main()
