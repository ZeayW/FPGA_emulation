import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ContainerRuntimeTest(unittest.TestCase):
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
            self.assertEqual(args[-3:], ["/versioned/sta", "-exit", "query path.tcl"])
            self.assertEqual(env["EMUFLOW_YOSYS_CONTAINER_BINARY"], "/versioned/yosys")
