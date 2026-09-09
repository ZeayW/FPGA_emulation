import copy
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.fixed_device import device_contract, materialize_device, validate_platform_device, vpr_device_arguments
from emuflow.platform import Platform
from emuflow.io import read_json, write_json
from emuflow.vtr_architecture import run_vtr_architecture_import
from emuflow.runtime import validate_physical_summary, PHYSICAL_SUMMARY_SCHEMA
from tests.native_build import vtr_architecture_importer

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "examples/architecture/vtr_k6_heterogeneous_fixture.xml"
BOARD = ROOT / "platforms/virtual/academic_vtr_4fpga_mesh.json"


class FixedDeviceTest(unittest.TestCase):
    def materialize(self, root, width=12):
        xml, board = root / "fixed.xml", root / "board.json"
        contract = materialize_device(ARCH, BOARD, xml, board, width, 14,
                                     executable=str(vtr_architecture_importer()))
        return xml, board, contract

    def test_roundtrip_and_native_single_layout_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml, board, contract = self.materialize(root)
            platform = Platform.load(board)
            self.assertEqual(validate_platform_device(platform, xml), contract)
            self.assertEqual(platform.to_dict()["physical_device"], contract)
            self.assertEqual(vpr_device_arguments(xml), ["--device", "emuflow_12x14"])
            self.assertEqual(len(platform.fpgas), 4)
            self.assertTrue(all(f.capacity == contract["capacity"] for f in platform.fpgas))
            self.assertGreater(contract["capacity"]["lut"], 0)
            self.assertGreater(contract["capacity"]["bram"], 0)
            # Hand-counted 10x12 interior: four 1x6 RAMs and three
            # 1x4 multipliers leave 84 CLBs, each with 10 LUT6 / 20 FF.
            self.assertEqual(contract["capacity"], {"lut": 840, "ff": 1680,
                                                    "bram": 4, "dsp": 3, "io": 352})
            run_vtr_architecture_import(input_path=xml, architecture_output_path=root / "arch.json",
                timing_output_path=root / "timing.json", architecture_id="test", width=12, height=14,
                executable=str(vtr_architecture_importer()))

    def test_capacity_tampering_and_wrong_architecture_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml, board, contract = self.materialize(Path(tmp))
            value = read_json(board)
            value["fpgas"][0]["capacity"]["lut"] += 1
            with self.assertRaisesRegex(ValidationError, "capacity"):
                validate_platform_device(Platform.from_dict(value), xml)
            xml.write_text(xml.read_text() + "\n")
            with self.assertRaisesRegex(ValidationError, "identity"):
                validate_platform_device(Platform.load(board), xml)

    def test_terminal_validator_rejects_a_resized_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, board, contract = self.materialize(Path(tmp))
            platform = Platform.load(board)
            summary = {"schema": PHYSICAL_SUMMARY_SCHEMA, "status": "pass",
                       "platform": platform.name, "design": "test", "physical_device": contract,
                       "fpgas": [{"fpga": f.id, "device_grid": {"width": 13, "height": 14}}
                                 for f in platform.fpgas]}
            with self.assertRaisesRegex(ValidationError, "grid"):
                validate_physical_summary(summary, {"design": "test"}, platform)

    def test_legacy_auto_capacity_is_not_a_fixed_device(self):
        with self.assertRaisesRegex(ValidationError, "fixed"):
            device_contract(ARCH)

    def test_no_silent_resize_or_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml, board, _ = self.materialize(root)
            with self.assertRaisesRegex(ValidationError, "new"):
                self.materialize(root)
            with self.assertRaisesRegex(ValidationError, "dimensions"):
                materialize_device(xml, board, root / "new.xml", root / "new.json", 13, 14,
                                   executable=str(vtr_architecture_importer()))
