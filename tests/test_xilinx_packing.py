import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.xilinx_packing import (
    exhaustive_minimum_slice_count,
    pack_xilinx_sites,
    validate_xilinx_packing,
)


def cell(cell_type, connections=None):
    connections = connections or {"I0": [1], "O": [2]}
    directions = {name: ("output" if name in {"O", "Q", "CO"} else "input") for name in connections}
    return {"type": cell_type, "port_directions": directions, "connections": connections}


class XilinxPackingTest(unittest.TestCase):
    def _mapped(self):
        cells = {
            f"lut{i}": cell("LUT6", {"I0": [100 + i], "O": [200 + i]})
            for i in range(9)
        }
        for i in range(17):
            cells[f"ff{i}"] = cell("FDRE", {"C": [10], "CE": ["1"], "R": ["0"], "D": [20+i], "Q": [50+i]})
        cells["carry"] = cell("CARRY8", {"CI": [300], "CO": [301]})
        cells["dsp"] = cell("DSP48E2", {"A": [310], "P": [311]})
        cells["bram"] = cell("RAMB36E2", {"ADDR": [320], "DO": [321]})
        cells["uram"] = cell("URAM288", {"ADDR": [330], "DOUT": [331]})
        cells["ground"] = cell("GND", {})
        return {"modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}}

    def test_packer_and_independent_checker(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(self._mapped()), encoding="utf-8")
            report = pack_xilinx_sites(mapped, packed, top="top")
            check = validate_xilinx_packing(mapped, packed, top="top")
        self.assertEqual(report["summary"]["cells"], 31)
        self.assertEqual(report["summary"]["cluster_kinds"]["slice"], 2)
        self.assertEqual(check["status"], "pass")

    def test_checker_rejects_duplicate_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(self._mapped()), encoding="utf-8")
            pack_xilinx_sites(mapped, packed, top="top")
            value = json.loads(packed.read_text(encoding="utf-8"))
            value["clusters"][1]["assignments"].append(value["clusters"][0]["assignments"][0])
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "more than once"):
                validate_xilinx_packing(mapped, packed, top="top")

    def test_mux_cone_is_packed_into_dedicated_bels(self):
        value = {"modules": {"top": {"attributes": {"top": "1"}, "cells": {
            "lut0": cell("LUT6", {"I0": [1], "O": [10]}),
            "lut1": cell("LUT6", {"I0": [2], "O": [11]}),
            "mux": cell("MUXF7", {"I0": [10], "I1": [11], "S": [3], "O": [12]}),
        }}}}
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(value), encoding="utf-8")
            report = pack_xilinx_sites(mapped, packed, top="top")
        assignments = report["clusters"][0]["assignments"]
        self.assertEqual(
            {item["bel"] for item in assignments},
            {"A6LUT", "B6LUT", "F7MUX_AB"},
        )

    def test_carry_cascade_is_certified_and_tampering_fails(self):
        value = {"modules": {"top": {"attributes": {"top": "1"}, "cells": {
            "carry0": cell("CARRY8", {"CI": [1], "CO": list(range(10, 18))}),
            "carry1": cell("CARRY8", {"CI": [17], "CO": list(range(20, 28))}),
        }}}}
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(value), encoding="utf-8")
            report = pack_xilinx_sites(mapped, packed, top="top")
            self.assertEqual(report["summary"]["cascade_links"], 1)
            self.assertEqual(report["cascade_chains"][0]["instances"], ["carry0", "carry1"])
            report["cascade_chains"][0]["instances"].reverse()
            packed.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "cascade certificate"):
                validate_xilinx_packing(mapped, packed, top="top")

    def test_muxf9_cone_uses_all_slice_wide_mux_bels(self):
        cells = {}
        net = 100
        mux7_names = []
        for index in range(4):
            lut_names = []
            for side in range(2):
                name = f"lut{index}_{side}"
                cells[name] = cell("LUT6", {"I0": [index * 2 + side + 1], "O": [net]})
                lut_names.append((name, net))
                net += 1
            mux = f"mux7_{index}"
            cells[mux] = cell("MUXF7", {
                "I0": [lut_names[0][1]], "I1": [lut_names[1][1]],
                "S": [20 + index], "O": [net],
            })
            mux7_names.append((mux, net))
            net += 1
        cells["mux8_bottom"] = cell("MUXF8", {
            "I0": [mux7_names[0][1]], "I1": [mux7_names[1][1]], "S": [30], "O": [net],
        })
        bottom_net = net
        net += 1
        cells["mux8_top"] = cell("MUXF8", {
            "I0": [mux7_names[2][1]], "I1": [mux7_names[3][1]], "S": [31], "O": [net],
        })
        top_net = net
        net += 1
        cells["mux9"] = cell("MUXF9", {
            "I0": [bottom_net], "I1": [top_net], "S": [32], "O": [net],
        })
        value = {"modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}}
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(value), encoding="utf-8")
            report = pack_xilinx_sites(mapped, packed, top="top")
        bels = {item["bel"] for item in report["clusters"][0]["assignments"]}
        self.assertEqual(
            bels,
            {
                "A6LUT", "B6LUT", "C6LUT", "D6LUT",
                "E6LUT", "F6LUT", "G6LUT", "H6LUT",
                "F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH",
                "F8MUX_BOT", "F8MUX_TOP", "F9MUX",
            },
        )

    def test_two_ramb18_cells_share_one_physical_site_mode(self):
        value = {"modules": {"top": {"attributes": {"top": "1"}, "cells": {
            "lower": cell("RAMB18E2", {"ADDR": [1], "DO": [2]}),
            "upper": cell("RAMB18E2", {"ADDR": [3], "DO": [4]}),
        }}}}
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(value), encoding="utf-8")
            report = pack_xilinx_sites(mapped, packed, top="top")
            validate_xilinx_packing(mapped, packed, top="top")
        self.assertEqual(len(report["clusters"]), 1)
        self.assertEqual(report["clusters"][0]["site_mode"], "RAMB18E2x2")
        self.assertEqual(
            {item["bel"] for item in report["clusters"][0]["assignments"]},
            {"RAMB18E2_L", "RAMB18E2_U"},
        )

    def test_exhaustive_oracle(self):
        self.assertEqual(exhaustive_minimum_slice_count([(8, 0), (1, 0)]), 2)
        self.assertEqual(exhaustive_minimum_slice_count([(4, 8), (4, 8)]), 1)
        self.assertEqual(exhaustive_minimum_slice_count([]), 0)


if __name__ == "__main__":
    unittest.main()
