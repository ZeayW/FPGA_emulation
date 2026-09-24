import json
import tempfile
import unittest
from pathlib import Path

from emuflow.amf_adapter import (
    export_amf_design,
    export_amf_fixture_device,
    import_amf_fixture_result,
    parse_amf_place_cell_tcl,
    validate_amf_fixture_roundtrip,
)
from emuflow.errors import ValidationError
from emuflow.xilinx_packing import pack_xilinx_sites
from emuflow.xilinx_placement import (
    place_xilinx_clusters,
    validate_xilinx_placement,
)


def _cell(cell_type, connections):
    directions = {
        port: ("output" if port in {"G", "P", "O", "O5", "O6", "Q", "CO"} else "input")
        for port in connections
    }
    return {
        "type": cell_type,
        "parameters": {},
        "port_directions": directions,
        "connections": connections,
    }


def _fixture_mapped():
    cells = {
        "ground": _cell("GND", {"G": [900]}),
        "power": _cell("VCC", {"P": [901]}),
        "lut_main": _cell("LUT2", {"I0": [900], "I1": [901], "O": [500]}),
        "ff_main": _cell(
            "FDRE",
            {"C": ["0"], "CE": ["1"], "R": ["0"], "D": [500], "Q": [501]},
        ),
    }
    di = []
    select = []
    for index in range(8):
        di_bit = 1000 + index * 2
        select_bit = di_bit + 1
        cells[f"carry_lut_{index}"] = _cell(
            "LUT6_2",
            {
                "I0": [900], "I1": [901], "I2": ["0"],
                "I3": ["0"], "I4": ["0"], "I5": ["1"],
                "O5": [di_bit], "O6": [select_bit],
            },
        )
        di.append(di_bit)
        select.append(select_bit)
    cells["carry"] = _cell(
        "CARRY8",
        {
            "CI": ["0"], "CI_TOP": ["0"], "DI": di, "S": select,
            "CO": list(range(1100, 1108)),
            "O": list(range(1200, 1208)),
        },
    )
    return {"modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}}


def _fixture_architecture():
    lut_cells = [*[f"LUT{width}" for width in range(1, 7)], "LUT6_2"]
    ff_cells = ["FDCE", "FDPE", "FDRE", "FDSE"]
    bels = []
    for index, letter in enumerate("ABCDEFGH"):
        bels.append({
            "name": f"{letter}6LUT", "type": "LUT6", "z": index,
            "compatible_cells": lut_cells,
        })
        bels.append({
            "name": f"{letter}FF", "type": "FF", "z": 8 + index,
            "compatible_cells": ff_cells,
        })
    bels.append({
        "name": "CARRY8", "type": "CARRY8", "z": 16,
        "compatible_cells": ["CARRY8"],
    })
    return {
        "schema": "emuflow.archdb/v1",
        "part": "amf-p2-fixture",
        "source": {"format": "unit-test/v1"},
        "policy": {"name": "amf-p2-fixture"},
        "site_templates": {
            "SLICEL": {"bels": bels, "alternative_templates": []},
        },
        "sites": [
            {
                "name": f"SLICE_X0Y{y}", "type": "SLICEL",
                "template": "SLICEL", "x": 0, "y": y,
                "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            }
            for y in range(2)
        ],
    }


def _result_tcl(packed):
    lines = ["set result [catch {place_cell {"]
    for cluster in sorted(packed["clusters"], key=lambda item: item["id"]):
        site = "SLICE_X0Y0" if cluster["kind"] == "carry" else "SLICE_X0Y1"
        for assignment in cluster["assignments"]:
            lines.append(
                f"  {assignment['instance']} {site}/{assignment['bel']}"
            )
    lines.append("}}]")
    return "\n".join(lines) + "\n"


class AMFAdapterTest(unittest.TestCase):
    def _packed(self, root):
        mapped = _fixture_mapped()
        mapped_path = root / "mapped.json"
        packed_path = root / "packed.json"
        mapped_path.write_text(json.dumps(mapped), encoding="utf-8")
        packed = pack_xilinx_sites(mapped_path, packed_path, top="top")
        return mapped, mapped_path, packed, packed_path

    def test_lut_ff_carry_roundtrip_and_independent_legalizer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, mapped_path, packed, packed_path = self._packed(root)
            architecture = _fixture_architecture()
            architecture_path = root / "architecture.json"
            architecture_path.write_text(json.dumps(architecture), encoding="utf-8")
            tcl = _result_tcl(packed)

            roundtrip = validate_amf_fixture_roundtrip(
                mapped=mapped,
                packed=packed,
                architecture_value=architecture,
                result_tcl=tcl,
                top="top",
            )
            self.assertEqual(roundtrip["status"], "pass")
            self.assertEqual(roundtrip["summary"]["normalized_constant_cells"], 2)
            self.assertEqual(roundtrip["summary"]["result_cells"], 11)

            constraints_path = root / "constraints.json"
            constraints_path.write_text(
                json.dumps(roundtrip["result_constraints"]), encoding="utf-8"
            )
            placement_path = root / "placement.json"
            place_xilinx_clusters(
                packed_path,
                architecture_path,
                placement_path,
                constraints_path=constraints_path,
            )
            checked = validate_xilinx_placement(
                packed_path,
                architecture_path,
                placement_path,
                constraints_path=constraints_path,
            )
            self.assertEqual(checked["status"], "pass")
            placement = json.loads(placement_path.read_text(encoding="utf-8"))
            actual = {
                (entry["instance"], entry["site"], entry["bel"])
                for cluster in placement["clusters"]
                for entry in cluster["assignments"]
            }
            imported = {
                (entry["instance"], entry["site"], entry["bel"])
                for entry in roundtrip["result_assignments"]
            }
            self.assertEqual(actual, imported)

            design = export_amf_design(mapped, top="top")
            self.assertNotIn("curCell=> ground", design["archive_text"])
            self.assertNotIn("curCell=> power", design["archive_text"])
            self.assertIn("net=> <const0> drivepin=> <const0>", design["archive_text"])
            self.assertIn("net=> <const1> drivepin=> <const1>", design["archive_text"])
            device = export_amf_fixture_device(architecture)
            self.assertEqual(len(device["archive_text"].splitlines()), 2)

    def test_unsupported_cores_and_production_geometry_fail_closed(self):
        mapped = _fixture_mapped()
        mapped["modules"]["top"]["cells"]["mux9"] = _cell(
            "MUXF9", {"I0": [1], "I1": [2], "S": [3], "O": [4]}
        )
        with self.assertRaisesRegex(ValidationError, "MUXF9"):
            export_amf_design(mapped, top="top")

        mapped = _fixture_mapped()
        mapped["modules"]["top"]["cells"]["uram"] = _cell("URAM288", {})
        with self.assertRaisesRegex(ValidationError, "URAM288"):
            export_amf_design(mapped, top="top")

        mapped = _fixture_mapped()
        mapped["modules"]["top"]["cells"]["lut_main"]["connections"]["O"] = [900]
        with self.assertRaisesRegex(ValidationError, "drives a constant net"):
            export_amf_design(mapped, top="top")

        architecture = _fixture_architecture()
        architecture["sites"][1]["physical_region"]["slr"] = "SLR1"
        with self.assertRaisesRegex(ValidationError, "multi-SLR"):
            export_amf_fixture_device(architecture)

        architecture = _fixture_architecture()
        architecture["part"] = "xcvu19p-open"
        with self.assertRaisesRegex(ValidationError, "XCVU19P"):
            export_amf_fixture_device(architecture)

    def test_result_parser_is_data_only_and_rejects_bad_ownership(self):
        parsed = parse_amf_place_cell_tcl(
            "puts hacked\nset result [catch {place_cell {\n"
            "  a SLICE_X0Y0/A6LUT\n}}]\nexec hacked\n"
        )
        self.assertEqual(parsed, {"a": ("SLICE_X0Y0", "A6LUT")})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, _mapped_path, packed, _packed_path = self._packed(root)
            architecture = _fixture_architecture()
            tcl = _result_tcl(packed).replace("/CARRY8", "/AFF", 1)
            with self.assertRaisesRegex(ValidationError, "illegal BEL"):
                import_amf_fixture_result(
                    tcl,
                    mapped=mapped,
                    packed=packed,
                    architecture_value=architecture,
                    top="top",
                )

            missing = "\n".join(_result_tcl(packed).splitlines()[:-2]) + "\n}}]\n"
            with self.assertRaisesRegex(ValidationError, "ownership differs"):
                import_amf_fixture_result(
                    missing,
                    mapped=mapped,
                    packed=packed,
                    architecture_value=architecture,
                    top="top",
                )

            overlap = _result_tcl(packed).replace("SLICE_X0Y1", "SLICE_X0Y0")
            with self.assertRaisesRegex(ValidationError, "overlap site"):
                import_amf_fixture_result(
                    overlap,
                    mapped=mapped,
                    packed=packed,
                    architecture_value=architecture,
                    top="top",
                )


if __name__ == "__main__":
    unittest.main()
