import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path

from emuflow.architecture import ArchitectureDB
from emuflow.errors import ValidationError
from emuflow.fpga_interchange import architecture_from_fpga_interchange_extract
from emuflow.io import read_json
from emuflow.rapidwright_provider import (
    RAPIDWRIGHT_GENERATOR_QUALIFICATION,
    rapidwright_producer_record,
    run_rapidwright_device_import,
    validate_rapidwright_architecture,
    validate_rapidwright_provider_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "examples/phase2/fpga_interchange_extract_fixture.json"
PINNED = (
    ROOT
    / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"
)


def fixture_manifest() -> dict:
    return {
        "schema": "emuflow.rapidwright-device-provider/v1",
        "provider_id": "rapidwright-xilinx-device-v1",
        "release": {
            "version": "2026.1.0",
            "tag": "v2026.1.0-beta",
            "revision": "1" * 40,
        },
        "device": {
            "part": "xcvu3p-ffvc1517-2-e",
            "device": "xcvu3p",
            "package": "ffvc1517",
            "speed_grade": "-2",
            "temperature_grade": "E",
        },
        "fpga_interchange": {
            "schema_revision": "2" * 40,
            "schema_license": "Apache-2.0",
            "generator_class": (
                "com.xilinx.rapidwright.interchange.DeviceResourcesExample"
            ),
        },
        "license": {
            "source_code": "Apache-2.0",
            "device_data": "Xilinx-EULA",
            "runtime": "mixed-license-external",
            "redistribution": "external-dependency-not-redistributed",
            "generated_device_data_committable": False,
        },
        "expected_physical_resources": {
            "CLB_LUT": 1,
            "CLB_FF": 1,
            "CARRY8": 1,
            "DSP48E2": 1,
            "RAMB18E2": 1,
            "RAMB36E2": 1,
        },
        "resource_evidence": {
            "document": "fixture data sheet",
            "revision": "v1",
            "url": "https://example.invalid/fixture",
            "table": "fixture resources",
        },
    }


class RapidWrightProviderTest(unittest.TestCase):
    def test_pinned_xcvu19p_provider_is_complete(self) -> None:
        report = validate_rapidwright_provider_manifest(read_json(PINNED))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["part"], "xcvu19p-fsva3824-2-e")
        self.assertEqual(
            report["expected_physical_resources"]["CLB_LUT"], 4085760
        )

    def test_release_device_and_license_mismatches_fail_closed(self) -> None:
        base = fixture_manifest()
        cases = [
            ("release", "tag", "v2026.1.0"),
            ("device", "package", "fsvd1517"),
            ("license", "device_data", "Apache-2.0"),
            ("license", "generated_device_data_committable", True),
        ]
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                manifest = copy.deepcopy(base)
                manifest[section][field] = value
                with self.assertRaises(ValidationError):
                    validate_rapidwright_provider_manifest(manifest)

    def test_import_is_sealed_to_provider_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "provider.json"
            manifest_path.write_text(
                json.dumps(fixture_manifest()), encoding="utf-8"
            )
            executable = root / "fake-importer"
            executable.write_text(
                """#!/usr/bin/env python3
import shutil
import sys
shutil.copyfile(sys.argv[1], sys.argv[2])
""",
                encoding="utf-8",
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            output = root / "architecture.json"
            report = run_rapidwright_device_import(
                input_path=EXTRACT,
                provider_manifest_path=manifest_path,
                output_path=output,
                executable=str(executable),
            )
            architecture = ArchitectureDB.load(output)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                report["rapidwright_validation"]["status"], "pass"
            )
            self.assertEqual(
                architecture.value["source"]["generator_qualification"],
                RAPIDWRIGHT_GENERATOR_QUALIFICATION,
            )

            tampered = fixture_manifest()
            tampered["expected_physical_resources"]["CLB_LUT"] = 2
            with self.assertRaisesRegex(
                ValidationError, "resource inventory"
            ):
                validate_rapidwright_architecture(
                    architecture, tampered, manifest_path=manifest_path
                )

    def test_direct_architecture_requires_producer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "provider.json"
            manifest = fixture_manifest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            producer = rapidwright_producer_record(
                manifest, manifest_path
            )
            architecture = architecture_from_fpga_interchange_extract(
                read_json(EXTRACT),
                part="xcvu3p-ffvc1517-2-e",
                input_path=EXTRACT,
                generator="RapidWright fixture",
                generator_qualification=RAPIDWRIGHT_GENERATOR_QUALIFICATION,
                producer=producer,
            )
            checked = validate_rapidwright_architecture(
                architecture, manifest, manifest_path=manifest_path
            )
            self.assertEqual(checked["status"], "pass")


if __name__ == "__main__":
    unittest.main()
