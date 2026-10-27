import json
from pathlib import Path
import tempfile
import unittest

from emuflow.common.errors import ValidationError
from scripts.validation.audit_xilinx_clock_capacity import audit_xilinx_clock_capacity


class ClockCapacityAuditTest(unittest.TestCase):
    def fixture(self, root, count):
        architecture = root / "architecture.json"
        architecture.write_text(json.dumps({
            "schema": "emuflow.archdb/v1", "part": "xcvu19p-test",
            "sites": [{"name": "SLICE_X0Y0", "physical_region": {"clock_region": "X0Y0"}}],
        }))
        rows = ["META\tpart\txcvu19p-test"]
        for i in range(count):
            rows.append(f"CELL\tc{i}\tff{i}\tFDRE\tSLICE_X0Y0\tAFF")
        for i in range(count):
            rows += [f"NET\tn{i}\tclock", f"PIN\tn{i}\tc{i}\tC\tsink"]
        path = root / "rwroute.tsv"
        path.write_text("\n".join(rows) + "\n")
        return path, architecture

    def test_boundary_is_24_distinct_clocks_per_region(self):
        with tempfile.TemporaryDirectory() as directory:
            for count, status in ((24, "pass"), (25, "fail")):
                with self.subTest(count=count):
                    paths = self.fixture(Path(directory), count)
                    report = audit_xilinx_clock_capacity(*paths)
                    self.assertEqual(report["status"], status)
                    self.assertEqual(report["metrics"]["maximum_distinct_clocks"], count)
                    self.assertEqual(report["over_capacity_regions"], ["X0Y0"] if count > 24 else [])

    def test_shared_clock_and_duplicate_pins_count_once_and_data_pins_do_not_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path, architecture = self.fixture(Path(directory), 1)
            with path.open("a") as stream:
                stream.write("PIN\tn0\tc0\tC\tsink\n")
                stream.write("PIN\tn0\tdata_only\tI0\tsink\n")
                for i in range(30):
                    stream.write(f"NET\tdata_clock_{i}\tclock\nPIN\tdata_clock_{i}\tdata_only\tD\tsink\n")
            report = audit_xilinx_clock_capacity(path, architecture)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["metrics"]["internal_clock_nets"], 1)
            self.assertEqual(report["metrics"]["clock_sink_cells"], 1)

    def test_missing_consumer_or_region_cannot_silently_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, architecture = self.fixture(root, 1)
            with path.open("a") as stream:
                stream.write("PIN\tn0\tmissing\tC\tsink\n")
            with self.assertRaisesRegex(ValidationError, "consumer CELL"):
                audit_xilinx_clock_capacity(path, architecture)
            path, architecture = self.fixture(root, 1)
            value = json.loads(architecture.read_text())
            value["sites"][0]["physical_region"] = None
            architecture.write_text(json.dumps(value))
            report = audit_xilinx_clock_capacity(path, architecture)
            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["coverage"]["complete"])
            self.assertEqual(report["metrics"]["unresolved_clock_sink_cells"], 1)

    def test_known_overload_is_preserved_when_other_regions_are_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            path, architecture = self.fixture(Path(directory), 25)
            with path.open("a") as stream:
                stream.write("CELL\tother\tother\tFDRE\tOTHER_SITE\tAFF\n")
                stream.write("NET\tother_clock\tclock\nPIN\tother_clock\tother\tC\tsink\n")
            report = audit_xilinx_clock_capacity(path, architecture)
            self.assertEqual(report["status"], "fail")
            self.assertFalse(report["coverage"]["complete"])
            self.assertEqual(report["metrics"]["maximum_distinct_clocks"], 25)
            self.assertEqual(report["over_capacity_regions"], ["X0Y0"])

    def test_device_identity_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path, architecture = self.fixture(Path(directory), 1)
            path.write_text(path.read_text().replace("xcvu19p-test", "other-part"))
            with self.assertRaisesRegex(ValidationError, "parts differ"):
                audit_xilinx_clock_capacity(path, architecture)
