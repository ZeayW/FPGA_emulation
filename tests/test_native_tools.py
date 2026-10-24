import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.errors import EmuFlowError
from emuflow.native_tools import (
    resolve_native_executable,
    resolve_native_stage_command,
)


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

    def test_openroad_logger_publishes_ostream_compatibility(self) -> None:
        cmake = (
            Path(__file__).resolve().parents[1]
            / "engines/openroad/src/utl/CMakeLists.txt"
        ).read_text(encoding="utf-8")
        public_interface = cmake.split(
            "target_compile_definitions(utl_lib", 1
        )[1].split(")", 1)[0]
        self.assertIn("PUBLIC", public_interface)
        self.assertIn("FMT_DEPRECATED_OSTREAM=1", public_interface)
        self.assertNotIn("target_compile_definitions(utl\n", cmake)

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

    def test_thin_runtime_rebuilds_unified_entry_from_exact_source(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts/validation/deploy_stream_runtime.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'unified="$runtime_dir/bin/emuflow-native"', script
        )
        self.assertIn("-DEMUFLOW_NATIVE_STAGE_LIBRARY=1", script)
        manifest = (Path(__file__).resolve().parents[1] / "src/native/emuflow_core/sources.txt").read_text()
        self.assertIn('emuflow_core/sources.txt', script)
        for source in (
            "static_exact_analyzer.cpp",
            "tlr_router.cpp",
            "tdm_exact_scheduler.cpp",
            "chimew_bank_channel_assigner.cpp",
            "chimew_rudy.cpp",
            "chimew_signal_grouper.cpp",
            "chimew_position_refiner.cpp",
            "emuflow_core/artifact_io.cpp",
            "emuflow_core/sqlite.cpp",
        ):
            self.assertIn(source, manifest)
        self.assertIn('emuflow_native.cpp', script)
        self.assertIn('"$archive"', script)

    def test_shared_core_build_owners_use_one_complete_manifest(self) -> None:
        from tests.native_build import _NATIVE_STAGE_SOURCES
        root = Path(__file__).resolve().parents[1]
        expected = {str(path.relative_to(root / "src/native"))
                    for path in (root / "src/native/emuflow_core").glob("*.cpp")}
        self.assertTrue(expected.issubset(set(_NATIVE_STAGE_SOURCES)))
        self.assertEqual(len(_NATIVE_STAGE_SOURCES), len(set(_NATIVE_STAGE_SOURCES)))
        cmake = (root / "CMakeLists.txt").read_text()
        self.assertIn('file(STRINGS "${EMUFLOW_CORE_MANIFEST}" EMUFLOW_CORE_SOURCE_NAMES)', cmake)
        self.assertIn('add_library(emuflow_core STATIC ${EMUFLOW_CORE_SOURCE_FILES})', cmake)
        self.assertNotIn('target_sources(emuflow_core', cmake)

    def test_actual_deployment_manifest_reader_under_strict_bash(self) -> None:
        import subprocess
        from tests.native_build import _NATIVE_STAGE_SOURCES
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/validation/deploy_stream_runtime.sh").read_text()
        block = script[script.index("core_sources=()"):script.index("objects=()")]
        command = 'set -eu\nsource_dir=$1\n' + block + '\nprintf "%s\\n" "${core_sources[@]}"\n'
        result = subprocess.run(['/bin/bash', '-c', command, 'manifest-reader', str(root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(_NATIVE_STAGE_SOURCES))
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp); manifest = source / 'src/native/emuflow_core/sources.txt'
            manifest.parent.mkdir(parents=True); (source / 'src/native/demo.cpp').write_text('')
            for invalid in ('demo.cpp\ndemo.cpp\n', '../escape.cpp\n', 'missing.cpp\n', '# no sources\n'):
                with self.subTest(manifest=invalid):
                    manifest.write_text(invalid)
                    failed = subprocess.run(['/bin/bash', '-c', command, 'manifest-reader', str(source)], capture_output=True, text=True)
                    self.assertNotEqual(failed.returncode, 0)

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

    def test_resolved_stage_does_not_require_legacy_when_unified_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unified = root / "bin" / "emuflow-native"
            unified.parent.mkdir()
            unified.write_text("#!/bin/sh\n", encoding="utf-8")
            unified.chmod(unified.stat().st_mode | stat.S_IXUSR)
            with patch.dict(
                "os.environ", {"EMUFLOW_NATIVE_ROOT": str(root)}, clear=True
            ), patch("emuflow.native_tools.REPO_ROOT", root / "missing"):
                self.assertEqual(
                    resolve_native_stage_command(
                        "phase6", "assign-banks"
                    ),
                    [str(unified.resolve()), "phase6", "assign-banks"],
                )

    def test_explicit_unified_entry_and_symlink_keep_stage_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unified = root / "emuflow-native"
            unified.write_text("#!/bin/sh\n")
            alias = root / "alias"
            alias.symlink_to(unified)
            for explicit in (unified, alias):
                self.assertEqual(
                    resolve_native_stage_command("phase3", "refine-patron", str(explicit)),
                    [str(unified.resolve()), "phase3", "refine-patron"],
                )

    def test_resolved_stage_preserves_explicit_comparison_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = root / "comparison"
            unified = root / "emuflow-native"
            for executable in (explicit, unified):
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(
                resolve_native_stage_command(
                    "phase5", "build-schedule", str(explicit)
                ),
                [str(explicit.resolve())],
            )

    def test_standalone_only_install_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "bin/emuflow_static_exact_analyzer"
            legacy.parent.mkdir()
            legacy.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy.chmod(legacy.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"EMUFLOW_NATIVE_ROOT": str(root)}, clear=True):
                with self.assertRaisesRegex(EmuFlowError, "in-tree emuflow-native build product"):
                    resolve_native_stage_command("phase3", "analyze")

    def test_configured_install_cannot_borrow_another_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            other = root / "other/build/native/install/bin/emuflow-native"
            other.parent.mkdir(parents=True)
            other.write_text("#!/bin/sh\n", encoding="utf-8")
            other.chmod(other.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"EMUFLOW_NATIVE_ROOT": str(root / "incomplete")}, clear=True), \
                    patch("emuflow.native_tools.REPO_ROOT", root / "other"):
                with self.assertRaisesRegex(EmuFlowError, "in-tree emuflow-native build product"):
                    resolve_native_stage_command("phase5", "build-schedule")

    def test_nonexecutable_unified_entry_does_not_select_standalone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "bin").mkdir()
            (root / "bin/emuflow-native").write_text("not executable")
            legacy = root / "bin/emuflow_tdm_exact_scheduler"
            legacy.write_text("#!/bin/sh\n")
            legacy.chmod(legacy.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"EMUFLOW_NATIVE_ROOT": str(root)}, clear=True):
                with self.assertRaisesRegex(EmuFlowError, "in-tree emuflow-native build product"):
                    resolve_native_stage_command("phase5", "build-schedule")


if __name__ == "__main__":
    unittest.main()
