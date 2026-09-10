import copy
import unittest

from emuflow.errors import ValidationError
from emuflow.snapshot_initial import mapped_snapshot_initial_state


def mapped():
    return {"modules": {"dut": {"netnames": {
        "q": {"bits": [2, 3, 4], "attributes": {"init": "x10"}}},
        "cells": {name: {"type": "$_DFF_P_", "connections": {"Q": [bit]}}
                  for name, bit in (("a", 2), ("b", 3), ("c", 4))}}}}


class SnapshotInitialTests(unittest.TestCase):
    def test_source_bits_preserved_and_undefined_explicit(self):
        document = mapped(); before = copy.deepcopy(document)
        state, report = mapped_snapshot_initial_state(document, top="dut", undefined_seed=1)
        self.assertEqual((state["a"], state["b"]), (0, 1))
        self.assertIn(state["c"], (0, 1))
        self.assertEqual(report["source_defined_bits"], 2)
        self.assertEqual(report["explicitly_seeded_undefined_bits"], 1)
        self.assertFalse(report["universal_reset_or_initial_state_proof"])
        self.assertEqual(document, before)
        document["modules"]["dut"]["cells"] = dict(reversed(list(document["modules"]["dut"]["cells"].items())))
        self.assertEqual(mapped_snapshot_initial_state(document, top="dut", undefined_seed=1)[0], state)

    def test_conflicting_aliases_and_bad_vectors_rejected(self):
        for init in ("1", "z", "00", 0):
            document = mapped()
            document["modules"]["dut"]["netnames"]["alias"] = {"bits": [2], "attributes": {"init": init}}
            with self.assertRaises(ValidationError):
                mapped_snapshot_initial_state(document, top="dut", undefined_seed=0)

    def test_unsupported_state_or_seed_not_silently_zeroed(self):
        for seed in (True, -1, None):
            with self.assertRaises(ValidationError):
                mapped_snapshot_initial_state(mapped(), top="dut", undefined_seed=seed)
        document = mapped(); document["modules"]["dut"]["cells"]["a"]["type"] = "$mem"
        with self.assertRaises(ValidationError):
            mapped_snapshot_initial_state(document, top="dut", undefined_seed=0)
