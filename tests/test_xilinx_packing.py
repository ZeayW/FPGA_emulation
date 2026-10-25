import hashlib
import json
import tempfile
import itertools
import random
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.xilinx_packing import (
    exhaustive_minimum_slice_count,
    legalize_xilinx_slice_site_pins,
    pack_xilinx_sites,
    validate_xilinx_packing,
    _minimum_cost_ff_assignment,
    _slice_ff_control_sets,
    _slice_site_pin_conflicts,
    _SliceSitePinContext,
)


def cell(cell_type, connections=None):
    connections = connections or {"I0": [1], "O": [2]}
    directions = {
        name: ("output" if name in {"O", "O5", "O6", "Q", "CO"} else "input")
        for name in connections
    }
    return {"type": cell_type, "port_directions": directions, "connections": connections}


def add_carry(cells, name, *, ci, co, net_base):
    di = []
    s = []
    for index in range(8):
        di_output = net_base + index * 2
        s_output = di_output + 1
        adapter = f"{name}$carry_lut6_2_{index}"
        cells[adapter] = cell("LUT6_2", {
            "I0": [net_base + 100 + index * 2],
            "I1": [net_base + 101 + index * 2],
            "I2": ["0"], "I3": ["0"], "I4": ["0"], "I5": ["1"],
            "O5": [di_output], "O6": [s_output],
        })
        di.append(di_output)
        s.append(s_output)
    cells[name] = cell("CARRY8", {
        "CI": [ci], "CI_TOP": ["0"], "DI": di, "S": s,
        "CO": list(range(co, co + 8)), "O": list(range(co + 8, co + 16)),
    })


class XilinxPackingTest(unittest.TestCase):
    def _internal_driver_mux(self, *, internal_select=False, remote_sink=True):
        cells = {
            "lut0": cell("LUT6", {"I0": [1], "O": [10]}),
            "lut1": cell("LUT6", {"I0": [2], "O": [11]}),
            "mux": cell("MUXF7", {"I0": [10], "I1": [11], "S": [10 if internal_select else 3], "O": [12]}),
            "ff": cell("FDRE", {"C": [50], "CE": ["1"], "R": ["0"],
                                   "D": [4 if internal_select else 10], "Q": [20]}),
        }
        clusters = [dict(id="local", kind="slice", assignments=[
            dict(instance=name, cell_type=cells[name]["type"], bel=bel)
            for name, bel in [("lut0", "A6LUT"), ("lut1", "B6LUT"),
                              ("mux", "F7MUX_AB"), ("ff", "AFF")]
        ])]
        if remote_sink:
            cells["remote"] = cell("FDRE", {"C": [50], "CE": ["1"], "R": ["0"], "D": [10], "Q": [21]})
            clusters.append(dict(id="remote", kind="slice", assignments=[
                dict(instance="remote", cell_type="FDRE", bel="AFF")]))
        return cells, clusters

    def test_local_driver_with_external_fanout_consumes_bypass_pin(self):
        for internal_select in (False, True):
            with self.subTest(internal_select=internal_select):
                cells, clusters = self._internal_driver_mux(internal_select=internal_select)
                context = _SliceSitePinContext(cells, clusters).collect(cells)
                self.assertIn(10, context.routed_bits)
                self.assertTrue(_slice_site_pin_conflicts(cells, clusters, context.drivers, context.routed_bits))
                report = legalize_xilinx_slice_site_pins(cells, clusters)
                self.assertEqual(report["moved_ffs"], 1)
                self.assertFalse(_slice_site_pin_conflicts(cells, clusters, context.drivers, context.routed_bits))

    def test_purely_local_driver_keeps_dedicated_ff_connection(self):
        cells, clusters = self._internal_driver_mux(remote_sink=False)
        context = _SliceSitePinContext(cells, clusters).collect(cells)
        self.assertNotIn(10, context.routed_bits)
        self.assertEqual(legalize_xilinx_slice_site_pins(cells, clusters)["moved_ffs"], 0)

    def test_indexed_pin_scope_matches_reference_without_cell_iteration(self):
        cells, clusters = self._internal_driver_mux()
        expected = _SliceSitePinContext(cells, clusters).collect(cells)
        with sqlite3.connect(":memory:") as database:
            database.executescript("CREATE TABLE metadata(key TEXT,value TEXT); "
                                  "CREATE TABLE cell_pins(cell_id TEXT,net_bit INTEGER,direction INTEGER,value_kind INTEGER)")
            database.execute("INSERT INTO metadata VALUES ('ports_json','{}')")
            for name, value in cells.items():
                database.executemany("INSERT INTO cell_pins VALUES (?,?,?,0)",
                    [(name, bit, int(value["port_directions"][port] == "output"))
                     for port, values in value["connections"].items() for bit in values if isinstance(bit, int)])
            class Indexed(dict):
                _with_pins = staticmethod(lambda *args: None)
            indexed = Indexed(cells)
            indexed.connection = database
            with mock.patch.object(indexed, "items", side_effect=AssertionError("whole cell traversal")):
                actual = _SliceSitePinContext(indexed, clusters).collect(indexed)
            self.assertEqual(actual.drivers, expected.drivers)
            self.assertEqual(actual.routed_bits, expected.routed_bits)

    def test_bounded_ff_assignment_matches_exhaustive_oracle(self):
        rng = random.Random(17)
        for n in range(1, 5):
            for _ in range(20):
                costs = [[rng.choice([10**9, rng.randrange(40)]) for _ in range(5)]
                         for _ in range(n)]
                feasible = [sum(costs[i][bel] for i, bel in enumerate(candidate))
                            for candidate in itertools.permutations(range(5), n)
                            if all(costs[i][bel] < 10**9 for i, bel in enumerate(candidate))]
                actual = _minimum_cost_ff_assignment(costs)
                if not feasible:
                    self.assertIsNone(actual)
                else:
                    self.assertEqual(len(set(actual)), n)
                    self.assertEqual(sum(costs[i][bel] for i, bel in enumerate(actual)), min(feasible))

    def _occupied_halves_with_mux_pins(self, upper_count=2):
        cells = {
            **{f"lut{i}": cell("LUT6", {"I0": [1000 + i], "O": [10 + i]}) for i in range(4)},
            "mux_ab": cell("MUXF7", {"I0": [10], "I1": [11], "S": [3], "O": [20]}),
            "mux_cd": cell("MUXF7", {"I0": [12], "I1": [13], "S": [3], "O": [21]}),
            "mux_bottom": cell("MUXF8", {"I0": [20], "I1": [21], "S": [4], "O": [22]}),
        }
        assignments = [dict(instance=f"lut{i}", cell_type="LUT6", bel=f"{letter}6LUT")
                       for i, letter in enumerate("ABCD")]
        assignments += [dict(instance=name, cell_type=cells[name]["type"], bel=bel)
                        for name, bel in [("mux_ab", "F7MUX_AB"), ("mux_cd", "F7MUX_CD"),
                                          ("mux_bottom", "F8MUX_BOT")]]
        lower = [("AFF", 60), ("BFF", 60), ("AFF2", 61), ("BFF2", 61)]
        upper = [(bel, 62) for bel in (["EFF2", "FFF2"] if upper_count == 2
                                      else [f"{letter}{suffix}" for letter in "EFGH" for suffix in ["FF", "FF2"]])]
        for index, (bel, enable) in enumerate([*lower, *upper]):
            name = f"ff{index}"
            cells[name] = cell("FDRE", {"C": [50 if index < 4 else 51], "CE": [enable],
                                        "R": ["0"], "D": [100 + index], "Q": [200 + index]})
            assignments.append(dict(instance=name, cell_type="FDRE", bel=bel))
        cluster = dict(id="site0", kind="slice", assignments=assignments)
        return cells, [cluster]

    def test_site_pin_repair_jointly_exchanges_occupied_clock_halves(self):
        cells, clusters = self._occupied_halves_with_mux_pins()
        before = json.dumps(cells, sort_keys=True)
        fixed = {a["instance"]: a["bel"] for a in clusters[0]["assignments"]
                 if a["cell_type"] != "FDRE"}
        _slice_ff_control_sets(cells, clusters[0]["assignments"])
        self.assertEqual(len(_slice_site_pin_conflicts(cells, clusters, {})), 2)
        report = legalize_xilinx_slice_site_pins(cells, clusters)
        self.assertEqual(report["moved_ffs"], 6)
        self.assertFalse(_slice_site_pin_conflicts(cells, clusters, {}))
        _slice_ff_control_sets(cells, clusters[0]["assignments"])
        for assignment in clusters[0]["assignments"]:
            name = assignment["instance"]
            if name in fixed:
                self.assertEqual(assignment["bel"], fixed[name])
            else:
                self.assertEqual(assignment["bel"][0] in "EFGH", int(name[2:]) < 4)
        self.assertEqual(json.dumps(cells, sort_keys=True), before)
        again_cells, again_clusters = self._occupied_halves_with_mux_pins()
        self.assertEqual(legalize_xilinx_slice_site_pins(again_cells, again_clusters), report)
        self.assertEqual(again_clusters, clusters)
        self.assertEqual(legalize_xilinx_slice_site_pins(cells, clusters)["moved_ffs"], 0)

    def test_site_pin_repair_still_rejects_infeasible_halves(self):
        cells, clusters = self._occupied_halves_with_mux_pins(upper_count=8)
        before = json.dumps(clusters, sort_keys=True)
        _slice_ff_control_sets(cells, clusters[0]["assignments"])
        with self.assertRaisesRegex(ValidationError, "cannot be legalized within certified"):
            legalize_xilinx_slice_site_pins(cells, clusters)
        self.assertEqual(json.dumps(clusters, sort_keys=True), before)

    def _mapped(self):
        cells = {
            f"lut{i}": cell("LUT6", {"I0": [100 + i], "O": [200 + i]})
            for i in range(9)
        }
        for i in range(17):
            cells[f"ff{i}"] = cell("FDRE", {"C": [10], "CE": ["1"], "R": ["0"], "D": [20+i], "Q": [50+i]})
        add_carry(cells, "carry", ci=300, co=301, net_base=1000)
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
        self.assertEqual(report["summary"]["cells"], 39)
        self.assertEqual(report["summary"]["cluster_kinds"]["slice"], 2)
        self.assertEqual(check["status"], "pass")

    def test_packer_reuses_in_memory_mapped_netlist_and_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            value = self._mapped()
            mapped.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch(
                "emuflow.xilinx_packing.read_json",
                side_effect=AssertionError("mapped netlist was reparsed"),
            ):
                report = pack_xilinx_sites(
                    mapped,
                    packed,
                    top="top",
                    mapped_value=value,
                    mapped_sha256="a" * 64,
                )
        self.assertEqual(report["source"]["mapped_json_sha256"], "a" * 64)
        self.assertEqual(report["summary"]["cells"], 39)

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

    def test_checker_rejects_wrong_carry_adapter_bel(self):
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(self._mapped()), encoding="utf-8")
            pack_xilinx_sites(mapped, packed, top="top")
            value = json.loads(packed.read_text(encoding="utf-8"))
            carry = next(
                cluster for cluster in value["clusters"]
                if cluster["kind"] == "carry"
            )
            carry["assignments"][0]["bel"] = "A5LUT"
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                ValidationError, "CARRY8 LUT6_2 packing topology"
            ):
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

    def test_site_pin_legalizer_moves_primary_ff_off_mux_select_pin(self):
        cells = {
            "lut0": cell("LUT6", {"I0": [1], "O": [10]}),
            "lut1": cell("LUT6", {"I0": [2], "O": [11]}),
            "mux": cell(
                "MUXF7", {"I0": [10], "I1": [11], "S": [3], "O": [12]}
            ),
            "ff": cell(
                "FDRE", {"C": [50], "CE": [51], "R": [52], "D": [4], "Q": [13]}
            ),
            "sel_src": cell("LUT1", {"I0": [5], "O": [3]}),
            "data_src": cell("LUT1", {"I0": [6], "O": [4]}),
        }
        mapped_value = {
            "modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}
        }
        control_set = json.dumps(
            {
                "clock": [50], "control": [52], "enable": [51],
                "parameters": {}, "type": "FDRE",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        clusters = [
            {
                "id": "slice0", "kind": "slice",
                "site_templates": ["SLICEL"], "control_set": control_set,
                "assignments": [
                    {"instance": "lut0", "cell_type": "LUT6", "bel": "A6LUT"},
                    {"instance": "lut1", "cell_type": "LUT6", "bel": "B6LUT"},
                    {"instance": "mux", "cell_type": "MUXF7", "bel": "F7MUX_AB"},
                    {"instance": "ff", "cell_type": "FDRE", "bel": "AFF"},
                ],
            },
            {
                "id": "slice1", "kind": "slice",
                "site_templates": ["SLICEL"], "control_set": None,
                "assignments": [
                    {"instance": "sel_src", "cell_type": "LUT1", "bel": "A6LUT"},
                    {"instance": "data_src", "cell_type": "LUT1", "bel": "B6LUT"},
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            mapped = Path(temporary) / "mapped.json"
            packed = Path(temporary) / "packed.json"
            mapped.write_text(json.dumps(mapped_value), encoding="utf-8")
            value = {
                "schema": "emuflow.packed-site-netlist/v1", "status": "pass",
                "top": "top",
                "source": {
                    "mapped_json_sha256": hashlib.sha256(mapped.read_bytes()).hexdigest()
                },
                "clusters": clusters, "cascade_chains": [],
                "unplaced_constants": [],
            }
            packed.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "external site pin"):
                validate_xilinx_packing(mapped, packed, top="top")
            report = legalize_xilinx_slice_site_pins(cells, clusters)
            packed.write_text(json.dumps(value), encoding="utf-8")
            validate_xilinx_packing(mapped, packed, top="top")
        self.assertEqual(report["moved_ffs"], 1)
        ff_assignment = next(
            item for item in clusters[0]["assignments"]
            if item["instance"] == "ff"
        )
        self.assertEqual(ff_assignment["bel"], "AFF2")

    def test_carry_cascade_is_certified_and_tampering_fails(self):
        cells = {}
        add_carry(cells, "carry0", ci=1, co=10, net_base=1000)
        add_carry(cells, "carry1", ci=17, co=40, net_base=2000)
        value = {"modules": {"top": {"attributes": {"top": "1"}, "cells": cells}}}
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
