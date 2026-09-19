import copy
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from emuflow.architecture import ArchitectureDB
from emuflow.fpga_interchange import (
    FPGAIF_ARCH_POLICY,
    architecture_from_fpga_interchange_extract,
    check_ir_architecture_capacity,
    run_fpga_interchange_architecture_import,
    parse_xilinx_part_identity,
    validate_fpga_interchange_architecture,
)
from emuflow.io import read_json


ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "examples/phase2/fpga_interchange_extract_fixture.json"


class FpgaInterchangeArchitectureTest(unittest.TestCase):
    def test_complete_part_identity_is_required(self) -> None:
        identity = parse_xilinx_part_identity("xcvu19p-fsva3824-2-e")
        self.assertEqual(identity["device"], "xcvu19p")
        self.assertEqual(identity["package"], "fsva3824")
        self.assertEqual(identity["speed_grade"], "-2")
        self.assertEqual(identity["temperature_grade"], "E")
        with self.assertRaisesRegex(Exception, "complete device-package"):
            parse_xilinx_part_identity("xcvu19p")

    def test_extract_maps_soft_logic_and_hard_blocks(self) -> None:
        architecture = architecture_from_fpga_interchange_extract(
            read_json(EXTRACT),
            part="xcvu3p-ffvc1517-2-e",
            input_path=EXTRACT,
            generator="fixture",
        )
        summary = architecture.summary()
        checked = validate_fpga_interchange_architecture(architecture)
        self.assertEqual(summary["policy"], FPGAIF_ARCH_POLICY)
        self.assertEqual(summary["cell_slots"]["CARRY8"], 1)
        self.assertEqual(summary["cell_slots"]["DSP48E2"], 1)
        self.assertEqual(summary["cell_slots"]["RAMB18E2"], 1)
        self.assertEqual(summary["cell_slots"]["RAMB36E2"], 1)
        self.assertIn("site_templates", architecture.value)
        self.assertNotIn("bels", architecture.value["sites"][0])
        self.assertTrue(
            architecture.site_named("SLICE_X4Y7")["bels"]
        )
        slice_bels = architecture.site_named("SLICE_X4Y7")["bels"]
        six_lut = next(bel for bel in slice_bels if bel["name"] == "A6LUT")
        self.assertIn("LUT6_2", six_lut["compatible_cells"])
        self.assertEqual(checked["status"], "pass")
        self.assertEqual(
            checked["physical_region_qualification"],
            "not-encoded-by-fpga-interchange-device-resources-v1",
        )

    def test_extract_accepts_ultrascaleplus_f9mux_placement_bel(self) -> None:
        extract = copy.deepcopy(read_json(EXTRACT))
        slice_site = extract["tiles"][0]["sites"][0]
        slice_site["bels"].append({
            "name": "F9MUX",
            "type": "F9MUX",
            "z": 0,
            "compatible_cells": ["MUXF9"],
        })
        architecture = architecture_from_fpga_interchange_extract(
            extract,
            part="xcvu3p-ffvc1517-2-e",
            input_path=EXTRACT,
            generator="fixture",
        )
        bels = architecture.site_named("SLICE_X4Y7")["bels"]
        f9mux = next(bel for bel in bels if bel["name"] == "F9MUX")
        self.assertEqual(f9mux["type"], "F9MUX")
        self.assertEqual(f9mux["compatible_cells"], ["MUXF9"])
        self.assertEqual(f9mux["placement_mode"], "SLICEL")

    def test_native_runner_is_reloaded_by_independent_checker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "fake-importer"
            output = root / "architecture.json"
            executable.write_text(
                """#!/usr/bin/env python3
import shutil
import sys
shutil.copyfile(sys.argv[1], sys.argv[2])
""",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            report = run_fpga_interchange_architecture_import(
                input_path=EXTRACT,
                part="xcvu3p-ffvc1517-2-e",
                generator="fixture",
                output_path=output,
                executable=str(executable),
            )
            artifact = ArchitectureDB.load(output)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["checker"]["status"], "pass")
        self.assertRegex(
            artifact.value["source"]["sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            artifact.value["physical_region_model"]["slr_encoded"], False
        )

    def test_tampered_coordinate_is_rejected(self) -> None:
        architecture = architecture_from_fpga_interchange_extract(
            read_json(EXTRACT),
            part="xcvu3p-ffvc1517-2-e",
            input_path=EXTRACT,
            generator="fixture",
        ).to_dict()
        architecture["sites"][0]["x"] += 1
        with self.assertRaisesRegex(Exception, "coordinate transform"):
            validate_fpga_interchange_architecture(
                ArchitectureDB(architecture)
            )

    def test_extract_device_package_and_grade_must_match_part(self) -> None:
        extract = read_json(EXTRACT)
        with self.assertRaisesRegex(Exception, "extract.device"):
            architecture_from_fpga_interchange_extract(
                {**extract, "device": "xcvu9p"},
                part="xcvu3p-ffvc1517-2-e",
                input_path=EXTRACT,
                generator="fixture",
            )
        with self.assertRaisesRegex(Exception, "requested package"):
            architecture_from_fpga_interchange_extract(
                extract,
                part="xcvu3p-fsvd1517-2-e",
                input_path=EXTRACT,
                generator="fixture",
            )
        with self.assertRaisesRegex(Exception, "requested grade"):
            architecture_from_fpga_interchange_extract(
                extract,
                part="xcvu3p-ffvc1517-3-e",
                input_path=EXTRACT,
                generator="fixture",
            )

    def test_rapidwright_dash_prefixed_temperature_grade_is_normalized(
        self,
    ) -> None:
        extract = copy.deepcopy(read_json(EXTRACT))
        extract["packages"][0]["grades"][0]["temperature_grade"] = "-e"
        architecture = architecture_from_fpga_interchange_extract(
            extract,
            part="xcvu3p-ffvc1517-2-e",
            input_path=EXTRACT,
            generator="RapidWright fixture",
        )
        self.assertEqual(
            architecture.value["source"]["device_identity"][
                "temperature_grade"
            ],
            "E",
        )

    def test_capacity_checker_covers_hard_resources_and_rejects_unknown(self) -> None:
        architecture = architecture_from_fpga_interchange_extract(
            read_json(EXTRACT),
            part="xcvu3p-ffvc1517-2-e",
            input_path=EXTRACT,
            generator="fixture",
        )
        ir = SimpleNamespace(
            value={
                "design": {"name": "hard-resource-smoke"},
                "instances": [
                    {"type": "LUT6"},
                    {"type": "CARRY8"},
                    {"type": "DSP48E2"},
                    {"type": "RAMB36E2"},
                    {"type": "GND"},
                ],
            }
        )
        report = check_ir_architecture_capacity(architecture, ir)
        self.assertEqual(report["status"], "pass")
        self.assertNotIn("GND", report["required_cell_slots"])

        ir.value["instances"].append({"type": "BUFGCE"})
        report = check_ir_architecture_capacity(architecture, ir)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["unsupported_cell_types"], {"BUFGCE": 1})


if __name__ == "__main__":
    unittest.main()
