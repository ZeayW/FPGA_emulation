import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import EmuFlowError
from emuflow.native_tools import resolve_native_executable


class NativeToolsTest(unittest.TestCase):
    def test_yosys_build_does_not_depend_on_ambient_tcl(self) -> None:
        cmake = (Path(__file__).resolve().parents[1] / "CMakeLists.txt").read_text(
            encoding="utf-8"
        )
        yosys_block = cmake.split("if(EMUFLOW_BUILD_YOSYS)", 1)[1].split(
            "endif()\n\nif(EMUFLOW_BUILD_CUDD)", 1
        )[0]
        # The command-line assignment takes precedence over Yosys's default
        # ENABLE_TCL := 1 during configure, build, and install invocations.
        self.assertEqual(yosys_block.count("ENABLE_TCL=0"), 3)

    def test_yosys_build_inherits_versioned_flex_header_path(self) -> None:
        cmake = (Path(__file__).resolve().parents[1] / "CMakeLists.txt").read_text(
            encoding="utf-8"
        )
        flex_block = cmake.split("if(EMUFLOW_FLEX_INCLUDE_DIR)", 1)[1].split(
            "endif()", 1
        )[0]
        self.assertIn("CPATH=${EMUFLOW_FLEX_INCLUDE_DIR}:$ENV{CPATH}", flex_block)

    def test_yosys_build_uses_versioned_parser_generators(self) -> None:
        cmake = (Path(__file__).resolve().parents[1] / "CMakeLists.txt").read_text(
            encoding="utf-8"
        )
        parser_setup = cmake.split("set(EMUFLOW_PARSER_CMAKE_ARGS", 1)[1].split(
            "set(\n  EMUFLOW_EXTERNAL_JOBS", 1
        )[0]
        self.assertIn('"BISON=${EMUFLOW_BISON_EXECUTABLE}"', parser_setup)
        self.assertIn("PATH=${EMUFLOW_FLEX_BIN_DIR}:$ENV{PATH}", parser_setup)
        yosys_block = cmake.split("if(EMUFLOW_BUILD_YOSYS)", 1)[1].split(
            "endif()\n\nif(EMUFLOW_BUILD_CUDD)", 1
        )[0]
        self.assertEqual(yosys_block.count("${EMUFLOW_YOSYS_MAKE_ARGS}"), 3)
        self.assertEqual(
            yosys_block.count("${CMAKE_COMMAND}\" -E env"),
            2,
        )

    def test_openroad_timer_uses_explicit_streamed_formatter(self) -> None:
        timer = (
            Path(__file__).resolve().parents[1]
            / "engines/openroad/src/utl/src/timer.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("#include <fmt/ostream.h>", timer)
        self.assertIn("fmt::streamed(static_cast<const Timer&>(*this))", timer)

    def test_openroad_guide_rect_uses_explicit_streamed_formatter(self) -> None:
        guide = (
            Path(__file__).resolve().parents[1]
            / "engines/openroad/src/odb/src/db/dbGuide.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("#include <fmt/ostream.h>", guide)
        self.assertIn("fmt::streamed(box)", guide)

    def test_resolves_only_configured_in_tree_install_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "bin" / "yosys"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            with patch.dict(
                os.environ,
                {"EMUFLOW_NATIVE_ROOT": str(root), "PATH": "/does/not/matter"},
                clear=False,
            ):
                self.assertEqual(
                    resolve_native_executable("yosys"),
                    str(executable.resolve()),
                )

    def test_does_not_silently_use_path_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path_bin = root / "path-bin"
            path_bin.mkdir()
            executable = path_bin / "repart"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

            with patch.dict(
                os.environ,
                {
                    "EMUFLOW_NATIVE_ROOT": str(root / "empty"),
                    "PATH": str(path_bin),
                },
                clear=False,
            ), patch(
                "emuflow.native_tools.REPO_ROOT", root / "empty-repo"
            ):
                with self.assertRaisesRegex(
                    EmuFlowError, "in-tree repart build product"
                ):
                    resolve_native_executable("repart")

    def test_explicit_override_is_preserved_for_comparison_runs(self) -> None:
        self.assertEqual(
            resolve_native_executable("openroad", "/comparison/openroad"),
            "/comparison/openroad",
        )

    def test_relative_override_is_bound_before_backend_changes_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "build" / "bin" / "vpr"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(root)
                resolved = resolve_native_executable(
                    "vpr", "build/bin/vpr"
                )
            finally:
                os.chdir(previous)
        self.assertEqual(resolved, str(executable.resolve()))


if __name__ == "__main__":
    unittest.main()
