import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import torch

from openparf.ops.typed_hardblock_legalizer import TypedHardblockLegalizer


class _PlaceDB:
    def __init__(self, names):
        self.names = {name: index for index, name in enumerate(names)}

    def nameToInst(self, name):
        return self.names[name]


class _Data:
    def __init__(self, count):
        self.movable_range = (0, count)
        self.inst_lock_mask = torch.zeros(count, dtype=torch.bool)


def _site(name, resource, x, y):
    return {
        "site": name, "resource": resource, "x": x, "y": y, "z": 0,
        "claims": [f"site:{name}"],
    }


class TypedHardblockLegalizerTest(unittest.TestCase):
    def _operator(self, value, names, site_rows=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / "constraints.json"
        if site_rows is not None:
            database_path = root / "sites.sqlite3"
            with sqlite3.connect(database_path) as database:
                database.executescript("""
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    ) WITHOUT ROWID;
                    CREATE TABLE sites (
                        dense_x INTEGER NOT NULL, dense_y INTEGER NOT NULL,
                        physical_x INTEGER NOT NULL, physical_y INTEGER NOT NULL,
                        placement_x REAL NOT NULL, placement_y REAL NOT NULL,
                        site TEXT NOT NULL, PRIMARY KEY(dense_x, dense_y)
                    ) WITHOUT ROWID;
                    CREATE TABLE physical_sites (
                        resource TEXT NOT NULL, physical_site TEXT NOT NULL,
                        dense_x INTEGER NOT NULL, dense_y INTEGER NOT NULL,
                        slot INTEGER NOT NULL,
                        PRIMARY KEY(resource, physical_site)
                    ) WITHOUT ROWID;
                """)
                database.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("schema", "emuflow.openparf-atomic-site-database/v1"),
                        ("site_count", str(len(site_rows))),
                    ],
                )
                for dense_x, dense_y, site, x, y in site_rows:
                    database.execute(
                        "INSERT INTO sites VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (dense_x, dense_y, dense_x, dense_y, x, y, site),
                    )
                    database.execute(
                        "INSERT INTO physical_sites VALUES (?, ?, ?, ?, ?)",
                        ("LUT", site, dense_x, dense_y, 0),
                    )
            value["site_database"] = {
                "schema": "emuflow.openparf-atomic-site-database/v1",
                "file": database_path.name,
                "sites": len(site_rows),
            }
        path.write_text(json.dumps(value), encoding="utf-8")
        data = _Data(len(names))
        return TypedHardblockLegalizer(path, _PlaceDB(names), data), data

    def test_uses_global_placement_cost_and_avoids_overlap(self):
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [
                {
                    "id": "chain", "kind": "cascade", "resource": "DSP48E2",
                    "instances": ["d0", "d1"],
                    "windows": [
                        [_site("D0", "DSP48E2", 0, 0), _site("D1", "DSP48E2", 0, 1)],
                        [_site("D1", "DSP48E2", 0, 1), _site("D2", "DSP48E2", 0, 2)],
                    ],
                },
                {
                    "id": "singleton", "kind": "singleton", "resource": "DSP48E2",
                    "instances": ["d2"],
                    "windows": [
                        [_site("D0", "DSP48E2", 0, 0)],
                        [_site("D2", "DSP48E2", 0, 2)],
                    ],
                },
            ],
        }
        operator, data = self._operator(value, ["d0", "d1", "d2"])
        pos = torch.tensor([[0.0, 1.1, 0.0], [0.0, 2.1, 0.0], [0.0, 0.1, 0.0]])
        operator(pos)
        self.assertEqual(pos.tolist(), [[0.0, 1.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 0.0]])
        self.assertTrue(torch.all(data.inst_lock_mask))
        self.assertEqual(
            [item["site"] for item in operator.last_assignment],
            ["D1", "D2", "D0"],
        )

    def test_fails_closed_when_no_conflict_free_window_exists(self):
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [
                {"id": "a", "kind": "singleton", "resource": "URAM288", "instances": ["u0"],
                 "windows": [[_site("U0", "URAM288", 0, 0)]]},
                {"id": "b", "kind": "singleton", "resource": "URAM288", "instances": ["u1"],
                 "windows": [[_site("U0", "URAM288", 0, 0)]]},
            ],
        }
        operator, _data = self._operator(value, ["u0", "u1"])
        with self.assertRaisesRegex(RuntimeError, "no conflict-free window"):
            operator(torch.zeros((2, 3)))

    def test_rejects_duplicate_instance_ownership(self):
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [
                {"id": "a", "kind": "singleton", "resource": "DSP48E2", "instances": ["d0"],
                 "windows": [[_site("D0", "DSP48E2", 0, 0)]]},
                {"id": "b", "kind": "singleton", "resource": "DSP48E2", "instances": ["d0"],
                 "windows": [[_site("D1", "DSP48E2", 0, 1)]]},
            ],
        }
        with self.assertRaisesRegex(ValueError, "appears twice"):
            self._operator(value, ["d0"])

    def test_ramb18_halves_share_a_tile_but_whole_mode_conflicts(self):
        lower = {
            "site": "RAMB18_X0Y0", "resource": "RAMB18E2",
            "x": 3, "y": 4, "z": 0, "claims": ["bram:BRAM_X0Y0:lower"],
        }
        upper = {
            "site": "RAMB18_X0Y1", "resource": "RAMB18E2",
            "x": 3, "y": 4, "z": 1, "claims": ["bram:BRAM_X0Y0:upper"],
        }
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [
                {
                    "id": "lo", "kind": "singleton", "resource": "RAMB18E2", "instances": ["r0"],
                    "windows": [[lower], [upper]],
                },
                {
                    "id": "hi", "kind": "singleton", "resource": "RAMB18E2", "instances": ["r1"],
                    "windows": [[lower], [upper]],
                },
            ],
        }
        operator, _data = self._operator(value, ["r0", "r1"])
        pos = torch.tensor([[3.0, 4.0, 0.0], [3.0, 4.0, 1.0]])
        operator(pos)
        self.assertEqual({item["site"] for item in operator.last_assignment}, {
            "RAMB18_X0Y0", "RAMB18_X0Y1",
        })

        whole = {
            "site": "RAMB36_X0Y0", "resource": "RAMB36E2",
            "x": 3, "y": 4, "z": 0,
            "claims": ["bram:BRAM_X0Y0:lower", "bram:BRAM_X0Y0:upper"],
        }
        value["groups"].append({
            "id": "whole", "kind": "singleton", "resource": "RAMB36E2", "instances": ["b0"],
            "windows": [[whole]],
        })
        operator, _data = self._operator(value, ["r0", "r1", "b0"])
        with self.assertRaisesRegex(RuntimeError, "no conflict-free window"):
            operator(torch.tensor([
                [3.0, 4.0, 0.0], [3.0, 4.0, 1.0], [3.0, 4.0, 0.0],
            ]))

    def test_same_site_macro_allows_one_shared_exclusive_claim(self):
        def member(name, resource, z):
            return {
                "site": name, "resource": resource,
                "x": 2, "y": 3, "z": z, "claims": ["site:" + name],
            }

        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [{
                "id": "mux", "kind": "site_macro", "resource": "SLICE_MACRO",
                "owned_resources": ["MUXF7"],
                "instances": ["l0", "l1", "m0"],
                "windows": [[
                    member("SLICE_X0Y0", "LUT", 1),
                    member("SLICE_X0Y0", "LUT", 3),
                    member("SLICE_X0Y0", "MUXF7", 0),
                ]],
            }],
        }
        operator, data = self._operator(value, ["l0", "l1", "m0"])
        pos = torch.zeros((3, 3))
        operator.legalize_site_macros(pos)
        self.assertEqual(pos.tolist(), [
            [2.0, 3.0, 1.0], [2.0, 3.0, 3.0], [2.0, 3.0, 0.0],
        ])
        self.assertTrue(torch.all(data.inst_lock_mask))

    def test_compact_same_site_template_streams_indexed_windows(self):
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [{
                "id": "mux", "kind": "site_macro", "resource": "SLICE_MACRO",
                "owned_resources": ["MUXF7"],
                "instances": ["l0", "l1", "m0"],
                "window_count": 2,
                "window_template": {
                    "kind": "same-site-slice/v1", "site_resource": "LUT",
                    "members": [
                        {"resource": "LUT", "z": 1, "bel": "A6LUT"},
                        {"resource": "LUT", "z": 3, "bel": "B6LUT"},
                        {"resource": "MUXF7", "z": 0, "bel": "F7MUX_AB"},
                    ],
                },
            }],
        }
        operator, data = self._operator(
            value,
            ["l0", "l1", "m0"],
            site_rows=[
                (0, 0, "SLICE_X0Y0", 0.0, 0.0),
                (1, 0, "SLICE_X1Y0", 9.0, 4.0),
            ],
        )
        pos = torch.tensor([
            [1.4, 0.5, 0.0], [1.6, 0.5, 0.0], [1.5, 0.4, 0.0],
        ])
        operator.legalize_site_macros(pos)
        self.assertEqual(pos.tolist(), [
            [0.0, 0.0, 1.0], [0.0, 0.0, 3.0], [0.0, 0.0, 0.0],
        ])
        self.assertTrue(torch.all(data.inst_lock_mask))

    def test_compact_carry8_site_template_is_a_native_slice_resource(self):
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [{
                "id": "carry", "kind": "site_macro",
                "resource": "SLICE_MACRO", "owned_resources": ["CARRY8"],
                "instances": ["carry", "lut"], "window_count": 2,
                "window_template": {
                    "kind": "same-site-slice/v1", "site_resource": "LUT",
                    "members": [
                        {"resource": "CARRY8", "z": 0, "bel": "CARRY8"},
                        {"resource": "LUT", "z": 1, "bel": "A6LUT"},
                    ],
                },
            }],
        }
        operator, data = self._operator(
            value,
            ["carry", "lut"],
            site_rows=[
                (0, 0, "SLICE_X0Y0", 0.0, 0.0),
                (1, 0, "SLICE_X1Y0", 9.0, 4.0),
            ],
        )
        pos = torch.tensor([[1.4, 0.5, 0.0], [1.6, 0.5, 0.0]])
        operator.legalize_site_macros(pos)
        self.assertEqual(pos.tolist(), [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        self.assertTrue(torch.all(data.inst_lock_mask))

    def test_compact_site_search_is_exact_cached_and_sublinear(self):
        def group(group_id, instances):
            return {
                "id": group_id, "kind": "site_macro",
                "resource": "SLICE_MACRO", "owned_resources": ["CARRY8"],
                "instances": instances, "window_count": 400,
                "window_template": {
                    "kind": "same-site-slice/v1", "site_resource": "LUT",
                    "members": [
                        {"resource": "CARRY8", "z": 0, "bel": "CARRY8"},
                        {"resource": "LUT", "z": 1, "bel": "A6LUT"},
                    ],
                },
            }

        names = ["carry0", "lut0", "carry1", "lut1"]
        value = {
            "schema": "openparf.physical-macro-groups/v2", "status": "pass",
            "groups": [
                group("a", names[:2]),
                group("b", names[2:]),
            ],
        }
        rows = [
            (x, y, "SLICE_X{}Y{}".format(x, y), float(x), float(y))
            for x in range(20) for y in range(20)
        ]
        operator, data = self._operator(value, names, site_rows=rows)
        original_cost = operator._cost
        evaluations = []

        def counted_cost(*args):
            evaluations.append(1)
            return original_cost(*args)

        operator._cost = counted_cost
        pos = torch.tensor([
            [10.5, 10.5, 0.0], [10.5, 10.5, 0.0],
            [10.5, 10.5, 0.0], [10.5, 10.5, 0.0],
        ])
        operator.legalize_site_macros(pos)
        assigned = [item["site"] for item in operator.last_assignment]
        # The four distance-one alternatives are tied geometrically; retain
        # the original exhaustive legalizer's lexicographic site-name tie.
        self.assertEqual(set(assigned), {"SLICE_X10Y10", "SLICE_X10Y11"})
        self.assertEqual(len(operator._compact_site_indexes), 1)
        self.assertLess(len(evaluations), 20)
        self.assertTrue(torch.all(data.inst_lock_mask))


if __name__ == "__main__":
    unittest.main()
