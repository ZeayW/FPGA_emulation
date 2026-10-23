import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ContainerRuntimeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cmake"), "requires CMake")
    def test_tcl_header_guard_handles_upstream_preprocessor_spacing(self):
        helper = Path(__file__).resolve().parents[1] / "engines/opensta/cmake/FindTCL.cmake"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header, script = root / "tcl.h", root / "probe.cmake"
            script.write_text(f'set(TCL_HEADER "{header}")\nset(TCL_LIBRARY "{root}/libtcl.so")\ninclude("{helper}")\n')
            for minor, expected in ((5, False), (6, True), (7, True)):
                with self.subTest(minor=minor):
                    header.write_text(f"#   define TCL_MAJOR_VERSION   8\n#define TCL_MINOR_VERSION   {minor}\n")
                    run = subprocess.run([shutil.which("cmake"), "-P", str(script)], text=True, capture_output=True)
                    self.assertEqual(run.returncode == 0, expected, run.stdout + run.stderr)

    def test_clean_environment_retains_sta_artifact_bindings_and_exact_arguments(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            mock_container = scratch / "container"
            mock_container.write_text(
                f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            mock_container.chmod(0o755)
            env = {"PATH": os.defpath, "TMPDIR": str(scratch),
                "EMUFLOW_YOSYS_CONTAINER_EXECUTABLE": str(mock_container),
                "EMUFLOW_YOSYS_CONTAINER_IMAGE": "/versioned/image.sif",
                "EMUFLOW_YOSYS_CONTAINER_BINARY": "/versioned/yosys",
                "EMUFLOW_OPENSTA_CONTAINER_BINARY": "/versioned/sta",
                "EMUFLOW_OPENSTA_CONTAINER_TCL_LIBRARY": "/versioned/tcl library",
                "EMUFLOW_OPENROAD_CONTAINER_BINARY": "/versioned/openroad",
                "EMUFLOW_OPENROAD_CONTAINER_TCL_LIBRARY": "/versioned/openroad tcl",
                "EMUFLOW_YOSYS_CONTAINER_BIND": str(scratch),
                "EMUFLOW_YOSYS_CONTAINER_LIBRARY_PATH": "/versioned/lib",
                "EMUFLOW_STA_VERILOG": str(scratch / "net list.v"),
                "EMUFLOW_STA_TOP": "top with spaces"}
            run = subprocess.run([str(root / "scripts/opensta/container_runtime.sh"), "-exit", "query path.tcl"],
                                 env=env, text=True, capture_output=True, check=True)
            args = json.loads(run.stdout)
            self.assertIn("--cleanenv", args)
            self.assertIn("EMUFLOW_STA_VERILOG=" + env["EMUFLOW_STA_VERILOG"], args)
            self.assertIn("EMUFLOW_STA_TOP=top with spaces", args)
            self.assertIn("TCL_LIBRARY=/versioned/tcl library", args)
            self.assertEqual(args[-3:], ["/versioned/sta", "-exit", "query path.tcl"])
            self.assertEqual(env["EMUFLOW_YOSYS_CONTAINER_BINARY"], "/versioned/yosys")
            road = subprocess.run([str(root / "scripts/openroad/container_runtime.sh"), "-exit", "partition.tcl"],
                                  env=env, text=True, capture_output=True, check=True)
            road_args = json.loads(road.stdout)
            self.assertIn("TCL_LIBRARY=/versioned/openroad tcl", road_args)
            self.assertEqual(road_args[-3:], ["/versioned/openroad", "-exit", "partition.tcl"])
