"""Preflight control-flow tests; these do not qualify a vendor device."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/reference_hardware_preflight.tcl"


@unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter required")
class ReferenceHardwarePreflightTest(unittest.TestCase):
    def probe(self, *, device=True, gty=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "harness.tcl"
            harness.write_text(
                'set argc 2\n'
                f'set argv [list xcvu9p-flga2104-2L-e {{{root / "out"}}}]\n'
                'proc create_project {args} {}\n'
                'set opened 0\n'
                'proc current_fileset {} {return sources_1}\n'
                'proc set_property {args} {set ::mode [lindex $args 1]}\n'
                'proc open_io_design {args} {\n'
                '  if {$::mode ne "PinPlanning"} {error "wrong design mode"}\n'
                '  set ::opened 1\n'
                '}\n'
                'proc close_project {} {}\n'
                'proc version {args} {return mock-not-vivado}\n'
                f'proc get_parts {{args}} {{return {{{"part" if device else ""}}}}}\n'
                'proc get_sites {args} {\n'
                '  if {!$::opened} {error "device view must be opened first"}\n'
                f'  return {{{"site" if gty else ""}}}\n'
                '}\n'
                f'source {{{SCRIPT}}}\n'
            )
            result = subprocess.run([shutil.which("tclsh"), str(harness)],
                                    capture_output=True, text=True, timeout=10)
            report = root / "out/device-preflight.tsv"
            return result, report.read_text() if report.exists() else None

    def test_device_probe_does_not_claim_physical_or_license_success(self):
        result, report = self.probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("implementation_license\tnot_checked", report)
        self.assertIn("physical_implementation\tnot_run", report)
        self.assertIn("board_wiring\tnot_verified", report)

    def test_missing_device_or_transceivers_cannot_emit_pass(self):
        for options in ({"device": False}, {"gty": False}):
            with self.subTest(options=options):
                result, report = self.probe(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(report)
