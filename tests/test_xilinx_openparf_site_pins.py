import json
from pathlib import Path
import tempfile
import unittest

from emuflow.errors import ValidationError
from emuflow.xilinx_openparf_atomic import (
    export_xilinx_openparf_atomic, legalize_xilinx_openparf_ff_control_sets,
    read_xilinx_openparf_atomic_name_map, validate_xilinx_openparf_atomic_placement,
)
from emuflow.xilinx_packing import SLICE_FF_BELS, pack_xilinx_sites
from tests.openparf_runtime_fixture import write_openparf_fixed_mux_fixture
from tests import test_xilinx_packing as packing_fixture


class AtomicSitePinTests(unittest.TestCase):
    def test_infeasible_mux_site_evicts_one_ff_and_recertifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped, packed, architecture = write_openparf_fixed_mux_fixture(root / "inputs")
            cells, clusters = packing_fixture.XilinxPackingTest()._occupied_halves_with_mux_pins(8)
            outputs = {bit for c in cells.values() for p, bits in c["connections"].items()
                       if c["port_directions"][p] == "output" for bit in bits if isinstance(bit, int)}
            inputs = sorted({bit for c in cells.values() for p, bits in c["connections"].items()
                             if c["port_directions"][p] == "input" for bit in bits
                             if isinstance(bit, int)} - outputs)
            mapped.write_text(json.dumps({"modules": {"top": {
                "attributes": {"top": "1"}, "cells": cells,
                "ports": {"inputs": {"direction": "input", "bits": inputs}}, "netnames": {},
            }}}))
            pack_xilinx_sites(mapped, packed, top="top")
            native = root / "native"
            export_xilinx_openparf_atomic(mapped, packed, architecture, native)
            name_map = read_xilinx_openparf_atomic_name_map(native / "name_map.json")
            roles = {a["instance"]: a["bel"] for a in clusters[0]["assignments"]}
            mux_roles = {"F7MUX_AB": 0, "F7MUX_CD": 1, "F8MUX_BOT": 0}
            rows = []
            fixed = {}
            for atom in name_map["atoms"]:
                bel = roles[atom["instance"]]
                z = (SLICE_FF_BELS.index(bel) if atom["resource"] == "FF"
                     else 2 * "ABCDEFGH".index(bel[0]) + 1 if atom["resource"] == "LUT"
                     else mux_roles[bel])
                row = f"{atom['openparf']} 0 0 {z}"
                rows.append(row)
                if atom["resource"] != "FF":
                    fixed[atom["openparf"]] = (0, 0, z)
            placement = native / "pressure.pl"
            placement.write_text("\n".join(rows) + "\n")
            with self.assertRaisesRegex(ValidationError, "site-pin legality"):
                validate_xilinx_openparf_atomic_placement(
                    placement, native / "name_map.json", mapped, architecture)
            report = legalize_xilinx_openparf_ff_control_sets(
                placement, native / "name_map.json", mapped, native / "ff.json", packed_path=packed)
            self.assertEqual(report["site_pin_evicted_ffs"], 1)
            self.assertGreater(report["site_pin_repacked_ffs"], 0)
            self.assertEqual(report["maximum_manhattan_distance"], 1)
            after = {row[0]: tuple(map(int, row[1:])) for row in
                     (line.split() for line in placement.read_text().splitlines())}
            self.assertEqual({name: after[name] for name in fixed}, fixed)
            validate_xilinx_openparf_atomic_placement(
                placement, native / "name_map.json", mapped, architecture)
            repeat = legalize_xilinx_openparf_ff_control_sets(
                placement, native / "name_map.json", mapped, native / "repeat.json", packed_path=packed)
            self.assertEqual(repeat["moved_ff_atoms"], 0)
