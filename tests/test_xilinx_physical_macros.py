import copy
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_physical_macros import (
    XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA,
    derive_xilinx_physical_macro_contract,
    validate_xilinx_physical_macro_contract,
)


def cell(cell_type, connections, outputs):
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def add_carry(cells, name, *, ci, co, net_base):
    di = []
    select = []
    for index in range(8):
        di_output = net_base + 2 * index
        select_output = di_output + 1
        adapter = f"{name}$lut6_2_{index}"
        cells[adapter] = cell(
            "LUT6_2",
            {
                "I0": [net_base + 100 + 2 * index],
                "I1": [net_base + 101 + 2 * index],
                "I2": ["0"],
                "I3": ["0"],
                "I4": ["0"],
                "I5": ["1"],
                "O5": [di_output],
                "O6": [select_output],
            },
            {"O5", "O6"},
        )
        di.append(di_output)
        select.append(select_output)
    cells[name] = cell(
        "CARRY8",
        {
            "CI": [ci],
            "CI_TOP": ["0"],
            "DI": di,
            "S": select,
            "CO": list(range(co, co + 8)),
            "O": list(range(co + 8, co + 16)),
        },
        {"CO", "O"},
    )


def add_muxf9(cells, *, net_base):
    net = net_base
    mux7_outputs = []
    for index in range(4):
        lut_outputs = []
        for side in range(2):
            name = f"mux_lut_{index}_{side}"
            cells[name] = cell(
                "LUT6", {"I0": [20 + 2 * index + side], "O": [net]}, {"O"}
            )
            lut_outputs.append(net)
            net += 1
        name = f"mux7_{index}"
        cells[name] = cell(
            "MUXF7",
            {"I0": [lut_outputs[0]], "I1": [lut_outputs[1]], "S": [40 + index], "O": [net]},
            {"O"},
        )
        mux7_outputs.append(net)
        net += 1
    mux8_outputs = []
    for index, pair in enumerate(((0, 1), (2, 3))):
        name = f"mux8_{index}"
        cells[name] = cell(
            "MUXF8",
            {
                "I0": [mux7_outputs[pair[0]]],
                "I1": [mux7_outputs[pair[1]]],
                "S": [50 + index],
                "O": [net],
            },
            {"O"},
        )
        mux8_outputs.append(net)
        net += 1
    cells["mux9"] = cell(
        "MUXF9",
        {"I0": [mux8_outputs[0]], "I1": [mux8_outputs[1]], "S": [60], "O": [net]},
        {"O"},
    )


def complete_fixture():
    cells = {}
    add_carry(cells, "carry0", ci=1, co=100, net_base=1000)
    add_carry(cells, "carry1", ci=107, co=200, net_base=2000)
    add_muxf9(cells, net_base=3000)
    cells["ram18_a"] = cell(
        "RAMB18E2",
        {"ADDR": [70], "CASDOUTA": [4050], "DO": [4000]},
        {"CASDOUTA", "DO"},
    )
    cells["ram18_z"] = cell(
        "RAMB18E2",
        {"CASDINA": [4050], "ADDR": [71], "CASDOUTA": [4051], "DO": [4001]},
        {"CASDOUTA", "DO"},
    )
    cells["dsp0"] = cell(
        "DSP48E2", {"A": [72], "ACOUT": [4100, 4101], "P": [4102]}, {"ACOUT", "P"}
    )
    cells["dsp1"] = cell(
        "DSP48E2",
        {"ACIN": [4100, 4101], "A": [73], "ACOUT": [4110, 4111], "P": [4112]},
        {"ACOUT", "P"},
    )
    cells["bram0"] = cell(
        "RAMB36E2", {"ADDR": [74], "CASDOUTA": [4200, 4201], "DO": [4202]}, {"CASDOUTA", "DO"}
    )
    cells["bram1"] = cell(
        "RAMB36E2",
        {"CASDINA": [4200, 4201], "ADDR": [75], "CASDOUTA": [4210, 4211], "DO": [4212]},
        {"CASDOUTA", "DO"},
    )
    cells["uram0"] = cell(
        "URAM288",
        {"ADDR": [76], "CAS_OUT_DOUT_A": [4300, 4301], "DOUT": [4302]},
        {"CAS_OUT_DOUT_A", "DOUT"},
    )
    cells["uram1"] = cell(
        "URAM288",
        {
            "CAS_IN_DOUT_A": [4300, 4301],
            "ADDR": [77],
            "CAS_OUT_DOUT_A": [4310, 4311],
            "DOUT": [4312],
        },
        {"CAS_OUT_DOUT_A", "DOUT"},
    )
    return {"modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}}


class XilinxPhysicalMacroContractTest(unittest.TestCase):
    def _derive(self, value):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        mapped = root / "mapped.json"
        contract = root / "physical-macros.json"
        mapped.write_text(json.dumps(value), encoding="utf-8")
        report = derive_xilinx_physical_macro_contract(mapped, contract, top="top")
        return mapped, contract, report

    def test_derives_compact_provider_neutral_contract(self):
        mapped, contract, report = self._derive(complete_fixture())
        check = validate_xilinx_physical_macro_contract(mapped, contract, top="top")
        self.assertEqual(report["schema"], XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA)
        self.assertEqual(check["status"], "pass")

        by_kind = {item["kind"]: item for item in report["site_macros"]}
        carry = by_kind["carry8-lut6_2"]
        self.assertEqual(len(carry["members"]), 9)
        self.assertEqual(len(carry["connections"]), 16)
        mux = by_kind["muxf9-cone"]
        self.assertEqual(len(mux["members"]), 15)
        self.assertEqual(len(mux["connections"]), 14)

        occupancies = [
            item for item in report["site_macros"]
            if item["kind"] == "ramb18-half-site-occupancy"
        ]
        self.assertEqual(len(occupancies), 2)
        self.assertEqual(
            occupancies[0]["members"][0]["physical_roles"],
            ["RAMB18E2_L", "RAMB18E2_U"],
        )
        self.assertEqual(
            occupancies[0]["relative_site"]["device_binding"],
            "adapter_required",
        )

        chains = {item["cell_type"]: item for item in report["cascade_chains"]}
        self.assertEqual(
            set(chains),
            {"CARRY8", "DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"},
        )
        for chain in chains.values():
            relation = chain["relative_sites"]
            self.assertEqual(relation["relation"], "ordered-native-cascade")
            self.assertEqual(relation["device_adjacency"]["status"], "adapter_required")
            self.assertNotIn("step", relation)
            self.assertNotIn("axis", relation)

        serialized = contract.read_text(encoding="utf-8")
        self.assertNotIn("4100", serialized)
        self.assertIn('"sha256"', serialized)

    def test_ramb18_occupancy_does_not_invent_name_based_pairing(self):
        _mapped, _contract, report = self._derive(complete_fixture())
        records = [
            item for item in report["site_macros"]
            if item["kind"] == "ramb18-half-site-occupancy"
        ]
        self.assertEqual([len(item["members"]) for item in records], [1, 1])
        self.assertTrue(all(not item["connections"] for item in records))

    def test_ordinary_atoms_are_not_copied_into_the_contract(self):
        value = complete_fixture()
        value["modules"]["top"]["cells"]["ordinary_lut"] = cell(
            "LUT6", {"I0": [88], "O": [4900]}, {"O"}
        )
        value["modules"]["top"]["cells"]["ordinary_ff"] = cell(
            "FDRE", {"C": [89], "CE": ["1"], "R": ["0"], "D": [4900], "Q": [4901]}, {"Q"}
        )
        _mapped, _contract, report = self._derive(value)
        owners = {
            item["instance"] for item in report["ownership"]["site_macros"]
        }
        self.assertNotIn("ordinary_lut", owners)
        self.assertNotIn("ordinary_ff", owners)

    def test_muxf7_and_muxf8_roots_have_complete_cones(self):
        fixtures = {
            "muxf7-cone": {
                "l0": cell("LUT6", {"I0": [1], "O": [10]}, {"O"}),
                "l1": cell("LUT6", {"I0": [2], "O": [11]}, {"O"}),
                "root": cell(
                    "MUXF7", {"I0": [10], "I1": [11], "S": [3], "O": [12]}, {"O"}
                ),
            },
            "muxf8-cone": {
                "l0": cell("LUT6", {"I0": [1], "O": [10]}, {"O"}),
                "l1": cell("LUT6", {"I0": [2], "O": [11]}, {"O"}),
                "l2": cell("LUT6", {"I0": [3], "O": [12]}, {"O"}),
                "l3": cell("LUT6", {"I0": [4], "O": [13]}, {"O"}),
                "m0": cell(
                    "MUXF7", {"I0": [10], "I1": [11], "S": [5], "O": [14]}, {"O"}
                ),
                "m1": cell(
                    "MUXF7", {"I0": [12], "I1": [13], "S": [6], "O": [15]}, {"O"}
                ),
                "root": cell(
                    "MUXF8", {"I0": [14], "I1": [15], "S": [7], "O": [16]}, {"O"}
                ),
            },
        }
        for kind, cells in fixtures.items():
            with self.subTest(kind=kind):
                value = {
                    "modules": {
                        "top": {"attributes": {"top": "1"}, "cells": cells}
                    }
                }
                _mapped, _contract, report = self._derive(value)
                self.assertEqual(len(report["site_macros"]), 1)
                self.assertEqual(report["site_macros"][0]["kind"], kind)
                member_count = 3 if kind == "muxf7-cone" else 7
                self.assertEqual(len(report["site_macros"][0]["members"]), member_count)
                self.assertEqual(
                    len(report["site_macros"][0]["connections"]), member_count - 1
                )

    def test_incomplete_carry_adapter_fails_closed(self):
        value = complete_fixture()
        value["modules"]["top"]["cells"]["carry0$lut6_2_0"]["connections"]["O5"] = [9999]
        with self.assertRaisesRegex(ValidationError, "incomplete LUT6_2"):
            self._derive(value)

    def test_unowned_lut6_2_fails_closed(self):
        value = complete_fixture()
        value["modules"]["top"]["cells"]["orphan"] = cell(
            "LUT6_2", {"I0": [90], "O5": [5000], "O6": [5001]}, {"O5", "O6"}
        )
        with self.assertRaisesRegex(ValidationError, "unowned instances: orphan"):
            self._derive(value)

    def test_incomplete_mux_cone_fails_closed(self):
        value = complete_fixture()
        del value["modules"]["top"]["cells"]["mux_lut_0_0"]
        with self.assertRaisesRegex(ValidationError, "has no local driver"):
            self._derive(value)

    def test_cascade_branch_fails_closed(self):
        value = complete_fixture()
        value["modules"]["top"]["cells"]["dsp2"] = cell(
            "DSP48E2", {"ACIN": [4100, 4101], "A": [91], "P": [5100]}, {"P"}
        )
        with self.assertRaisesRegex(ValidationError, "cascade branches"):
            self._derive(value)

    def test_partial_width_cascade_fails_closed(self):
        value = complete_fixture()
        value["modules"]["top"]["cells"]["dsp1"]["connections"]["ACIN"] = [4100]
        with self.assertRaisesRegex(ValidationError, "partial-width connection"):
            self._derive(value)

    def test_tampering_is_rejected_by_connectivity_rederivation(self):
        mapped, contract, _report = self._derive(complete_fixture())
        value = json.loads(contract.read_text(encoding="utf-8"))
        value["cascade_chains"][0]["members"][0]["role"] = "tail"
        contract.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "differs from mapped connectivity"):
            validate_xilinx_physical_macro_contract(mapped, contract, top="top")

    def test_json_schema_declares_the_versioned_contract(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas/xilinx-physical-macro-contract-v1.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA,
        )
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
