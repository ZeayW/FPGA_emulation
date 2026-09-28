"""Native adapter compilation/path regression (requires the pinned RapidWright jar)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class RootedRoutePathTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RAPIDWRIGHT_JAR"), "set RAPIDWRIGHT_JAR for native test")
    def test_production_path_extractor(self):
        root = Path(__file__).resolve().parents[1]
        jar = Path(os.environ["RAPIDWRIGHT_JAR"]).resolve(strict=True)
        java_home = os.environ.get("JAVA_HOME")
        java = str(Path(java_home) / "bin/java") if java_home else shutil.which("java")
        javac = str(Path(java_home) / "bin/javac") if java_home else shutil.which("javac")
        self.assertTrue(java and javac, "JDK required")
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([javac, "-cp", str(jar), "-d", directory,
                            str(root / "scripts/rapidwright/EmuFlowRWRoute.java"),
                            str(root / "tests/java/RootedRoutePathTest.java")], check=True)
            subprocess.run([java, "-cp", os.pathsep.join((directory, str(jar))),
                            "RootedRoutePathTest"], check=True)
