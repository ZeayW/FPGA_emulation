import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.xilinx_openparf_carry8 import (
    export_xilinx_openparf_carry8,
    validate_xilinx_openparf_carry8_placement,
)
from emuflow.xilinx_openparf_carry8_bridge import (
    materialize_xilinx_openparf_carry8_contract,
)
from emuflow.xilinx_placement import validate_xilinx_placement
from emuflow.xilinx_rwroute import export_rwroute_input
from tests.test_xilinx_openparf_carry8 import _write_fixture, _write_placement


def _qualified_fixture(root: Path):
    mapped, packed, architecture, native, provider = _write_fixture(root)
    output = root / "openparf"
    export_xilinx_openparf_carry8(
        mapped, packed, architecture, native, provider, output, top="top"
    )
    placement = root / "native.pl"
    _write_placement(placement, read_json(output / "name_map.json"))
    certificate_path = root / "certificate.json"
    certificate = validate_xilinx_openparf_carry8_placement(
        placement, output / "name_map.json", mapped, architecture,
        native, provider, certificate_path,
    )
    certificate["runtime_validation"] = "native-openparf"
    write_json(certificate_path, certificate, compact=True)
    return mapped, packed, architecture, certificate_path


class XilinxOpenparfCarry8BridgeTest(unittest.TestCase):
    def test_preserves_macros_and_feeds_rwroute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture, certificate = _qualified_fixture(root)
            output = root / "placement.json"
            report = materialize_xilinx_openparf_carry8_contract(
                mapped, packed, architecture, certificate, output, top="top"
            )
            validation = validate_xilinx_placement(packed, architecture, output)
            self.assertEqual(report["runtime_validation"], "native-openparf")
            self.assertEqual(report["cells"], 20)
            self.assertEqual(report["clusters"], 3)
            self.assertEqual(validation["cascade_chains"], 1)

            placement = read_json(output)
            self.assertEqual(
                placement["provider"],
                "openparf-native-carry8-full-slice-bridge-v1",
            )
            self.assertEqual(
                {item["cluster"] for item in placement["clusters"]},
                {item["id"] for item in read_json(packed)["clusters"]},
            )
            rwroute = root / "rwroute.tsv"
            route_report = export_rwroute_input(
                mapped, packed, output, rwroute
            )
            self.assertEqual(route_report["logical_cells"], 20)
            self.assertEqual(route_report["expanded_lut6_2_cells"], 16)
            self.assertTrue(rwroute.is_file())

    def test_source_cluster_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture, certificate = _qualified_fixture(root)
            value = read_json(certificate)
            value["clusters"][0]["assignments"][0]["source_cluster"] = "wrong"
            write_json(certificate, value, compact=True)
            with self.assertRaisesRegex(ValidationError, "source cluster"):
                materialize_xilinx_openparf_carry8_contract(
                    mapped, packed, architecture, certificate,
                    root / "placement.json", top="top",
                )

    def test_unqualified_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture, certificate = _qualified_fixture(root)
            value = json.loads(certificate.read_text(encoding="utf-8"))
            value["runtime_validation"] = "unverified"
            certificate.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "runtime-qualified"):
                materialize_xilinx_openparf_carry8_contract(
                    mapped, packed, architecture, certificate,
                    root / "placement.json", top="top",
                )


if __name__ == "__main__":
    unittest.main()
