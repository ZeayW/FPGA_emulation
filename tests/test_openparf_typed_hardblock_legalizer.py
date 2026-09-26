import json
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
    return {"site": name, "resource": resource, "x": x, "y": y, "z": 0}


class TypedHardblockLegalizerTest(unittest.TestCase):
    def _operator(self, value, names):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "constraints.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        data = _Data(len(names))
        return TypedHardblockLegalizer(path, _PlaceDB(names), data), data

    def test_uses_global_placement_cost_and_avoids_overlap(self):
        value = {
            "schema": "openparf.typed-hardblock-chains/v1", "status": "pass",
            "groups": [
                {
                    "id": "chain", "resource": "DSP48E2",
                    "instances": ["d0", "d1"],
                    "windows": [
                        [_site("D0", "DSP48E2", 0, 0), _site("D1", "DSP48E2", 0, 1)],
                        [_site("D1", "DSP48E2", 0, 1), _site("D2", "DSP48E2", 0, 2)],
                    ],
                },
                {
                    "id": "singleton", "resource": "DSP48E2",
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
            "schema": "openparf.typed-hardblock-chains/v1", "status": "pass",
            "groups": [
                {"id": "a", "resource": "URAM288", "instances": ["u0"],
                 "windows": [[_site("U0", "URAM288", 0, 0)]]},
                {"id": "b", "resource": "URAM288", "instances": ["u1"],
                 "windows": [[_site("U0", "URAM288", 0, 0)]]},
            ],
        }
        operator, _data = self._operator(value, ["u0", "u1"])
        with self.assertRaisesRegex(RuntimeError, "no conflict-free window"):
            operator(torch.zeros((2, 3)))

    def test_rejects_duplicate_instance_ownership(self):
        value = {
            "schema": "openparf.typed-hardblock-chains/v1", "status": "pass",
            "groups": [
                {"id": "a", "resource": "DSP48E2", "instances": ["d0"],
                 "windows": [[_site("D0", "DSP48E2", 0, 0)]]},
                {"id": "b", "resource": "DSP48E2", "instances": ["d0"],
                 "windows": [[_site("D1", "DSP48E2", 0, 1)]]},
            ],
        }
        with self.assertRaisesRegex(ValueError, "appears twice"):
            self._operator(value, ["d0"])


if __name__ == "__main__":
    unittest.main()
